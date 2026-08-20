import logging
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, FrozenSet, Iterable, Optional
from uuid import uuid4

from watchdog.events import FileMovedEvent, FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


DEFAULT_EVENT_DEBOUNCE_SECONDS = 2.0

DEFAULT_MONITORED_EXTENSIONS: FrozenSet[str] = frozenset()

_DEBOUNCE_CLEANUP_INTERVAL_SECONDS = 60.0

# Plafon dur pentru numărul de evenimente urmărite simultan de debouncer.
# Curățarea pe bază de timp rulează cel mult o dată la
# _DEBOUNCE_CLEANUP_INTERVAL_SECONDS, deci nu limitează cu nimic o rafală mai
# scurtă de atât (dezarhivare, build, copiere recursivă, criptare în masă).
# Plafonul mărginește vârful de memorie independent de ceas.
_DEBOUNCE_MAX_TRACKED_EVENTS = 10_000

FileEventCallback = Callable[[Dict[str, str]], None]


class FileMonitorError(Exception):
    """Eroare ridicată atunci când monitorizarea directoarelor nu poate porni."""
    pass


def normalize_file_path(file_path: str) -> str:
    """
    Normalizează calea unui fișier pentru raportare și comparare.

    Funcționează atât pe Windows, cât și pe Linux.
    """
    return os.path.abspath(file_path)


def build_file_event_payload(
    agent_id: str,
    event_type: str,
    file_path: str,
    agent_instance_id: str,
    occurred_at: Optional[str] = None,
    settle_wait_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Construiește payload-ul unui eveniment de fișier.

    occurred_at este momentul PRIMEI observații a fișierului, furnizat de
    SettleTracker (Decizia 1). Trebuie transmis explicit tocmai pentru că
    raportarea are loc cu întârziere, după stabilizare: calculat aici, ar fi
    momentul emiterii, nu al faptului, iar serverul ar ordona evenimentele
    greșit — exact eroarea pe care câmpul occurred_at a fost introdus s-o
    prevină (vezi nota din contracts/wire-contract.json).

    Rămâne opțional pentru compatibilitate cu apelurile care nu trec încă prin
    tracker; absent, se folosește ceasul de acum.

    settle_wait_ms, când este prezent, călătorește sub measurements — model
    separat structural tocmai ca să nu se amestece cu câmpurile care descriu
    fișierul. Niciun câmp de acolo nu are voie să intre într-o decizie de
    verdict sau de escaladare.
    """
    current_time = datetime.now(timezone.utc).isoformat()
    event_time = occurred_at or current_time
    normalized_path = normalize_file_path(file_path)

    descriptions = {
        "file_created": "New file detected in monitored directory",
        "file_modified": "File modified in monitored directory",
    }

    description = descriptions.get(event_type, "File system event detected")

    payload: Dict[str, Any] = {
        "client_event_id": str(uuid4()),
        "agent_id": agent_id,
        "agent_instance_id": agent_instance_id,
        "event_type": event_type,
        "occurred_at": event_time,
        "file_path": normalized_path,
        "description": f"{description} at {event_time}",
    }

    if settle_wait_ms is not None:
        payload["measurements"] = {"settle_wait_ms": settle_wait_ms}

    return payload


class EventDebouncer:
    """
    Reduce raportarea repetată a aceluiași eveniment într-un interval scurt.

    Unele aplicații și unele sisteme de operare pot genera mai multe evenimente
    pentru aceeași operație de scriere a unui fișier.

    Memoria este mărginită de două mecanisme independente, care răspund la
    întrebări diferite:
      - curățarea periodică (_cleanup_stale_entries) elimină ce a devenit
        irelevant, cel mult o dată la _DEBOUNCE_CLEANUP_INTERVAL_SECONDS;
      - plafonul de capacitate (_evict_over_capacity) mărginește câte intrări
        pot exista simultan, indiferent cât timp a trecut.
    """

    def __init__(
        self,
        interval_seconds: float = DEFAULT_EVENT_DEBOUNCE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_tracked_events: int = _DEBOUNCE_MAX_TRACKED_EVENTS,
    ):
        self.interval_seconds = interval_seconds
        self.max_tracked_events = max_tracked_events
        self._clock = clock
        # OrderedDict, nu dict: ordinea intrărilor este ordinea ultimei apariții
        # a fiecărei chei, deci capătul din față este întotdeauna candidatul
        # corect pentru evicțiune.
        self._last_seen: "OrderedDict[str, float]" = OrderedDict()
        self._last_cleanup_time = 0.0
        self._lock = Lock()

    def _cleanup_stale_entries(self, current_time: float) -> None:
        """Curăță intrările vechi din dicționarul de evenimente văzute recent.
           Această metodă este apelată periodic pentru a preveni creșterea necontrolată a memoriei.
        """
        if (current_time - self._last_cleanup_time) < _DEBOUNCE_CLEANUP_INTERVAL_SECONDS:
            return

        
        
        cutoff = current_time - self.interval_seconds * 2

        # Comprehensiunea păstrează ordinea intrărilor rămase, deci ordinea
        # folosită de _evict_over_capacity supraviețuiește curățării.
        self._last_seen = OrderedDict(
            (key, timestamp)
            for key, timestamp in self._last_seen.items()
            if timestamp > cutoff
        )
        self._last_cleanup_time = current_time

    def _evict_over_capacity(self) -> None:
        """Menține numărul de intrări sub plafon, eliminându-le pe cele mai vechi.

        Spre deosebire de curățarea periodică, nu se uită la ceas: mărginește
        vârful de memorie și în interiorul unei rafale mai scurte decât
        intervalul de curățare, unde garda de timp nu intervine deloc.

        Compromisul este acceptat conștient: o cheie evacuată înainte de
        expirarea ferestrei de debounce va fi văzută din nou ca eveniment nou,
        deci se poate raporta un duplicat. Un duplicat ocazional sub presiune
        extremă este preferabil unei creșteri nemărginite a memoriei pe
        endpoint.
        """
        while len(self._last_seen) > self.max_tracked_events:
            self._last_seen.popitem(last=False)

    def is_duplicate(self, event_type: str, file_path: str) -> bool:
        """Returnează True dacă evenimentul a fost observat recent."""
        event_key = f"{event_type}:{os.path.normcase(normalize_file_path(file_path))}"
        current_time = self._clock()

        with self._lock:
            self._cleanup_stale_entries(current_time)
            previous_time = self._last_seen.get(event_key)
            self._last_seen[event_key] = current_time
            # Reatribuirea unei chei existente nu îi schimbă poziția, deci
            # mutarea la capăt este necesară ca ordinea să rămână „ultima
            # apariție", nu „prima apariție".
            self._last_seen.move_to_end(event_key)
            self._evict_over_capacity()

        if previous_time is None:
            return False

        return (current_time - previous_time) < self.interval_seconds


class EDRFileEventHandler(FileSystemEventHandler):
    """Procesează evenimentele de fișier detectate de watchdog."""

    def __init__(
        self,
        agent_id: str,
        agent_instance_id: str,
        monitored_directories: Iterable[str],
        event_callback: FileEventCallback,
        logger: logging.Logger,
        monitored_extensions: FrozenSet[str] = DEFAULT_MONITORED_EXTENSIONS,
        debounce_seconds: float = DEFAULT_EVENT_DEBOUNCE_SECONDS,
    ):
        super().__init__()

        self.agent_id = agent_id
        self.agent_instance_id = agent_instance_id
        self.monitored_directories = tuple(
            normalize_file_path(directory)
            for directory in monitored_directories
            )
        self.event_callback = event_callback
        self.logger = logger
        self.monitored_extensions = monitored_extensions
        self.debouncer = EventDebouncer(debounce_seconds)


    def _is_in_monitored_directory(self, file_path: str) -> bool:
        """Verifică dacă fișierul se află într-unul dintre directoarele monitorizate."""
        normalized_path = normalize_file_path(file_path)
        
        for monitored_directory in self.monitored_directories:
            try:
                common_path = os.path.commonpath([normalized_path, monitored_directory])
            except ValueError:
                continue
            
            if os.path.normcase(common_path) == os.path.normcase(monitored_directory):
                return True

        return False


    def _is_relevant_file(self, file_path: str) -> bool:
        """
        Verifică dacă fișierul are o extensie care trebuie monitorizată.
        Un frozen set gol de extensii înseamnă că toate fișierele sunt relevante.
        """
        if not self.monitored_extensions:
            return True
        

        extension = Path(file_path).suffix.lower()
        return extension in self.monitored_extensions

    def on_created(self, event: FileSystemEvent) -> None:
        """Procesează apariția unui fișier nou."""
        if not event.is_directory:
            self._handle_file_event(os.fsdecode(event.src_path), "file_created")

    def on_modified(self, event: FileSystemEvent) -> None:
        """Procesează modificarea unui fișier existent."""
        if not event.is_directory:
            self._handle_file_event(os.fsdecode(event.src_path), "file_modified")

    def on_moved(self, event: FileSystemEvent) -> None:
        """
        Procesează mutarea unui fișier.
        Cand un fisier este copiat sau mutat dintr-un alt loc(ex: USB, Downloads)
        in direcotrul monitorizat, watchdog genereaza un eveniment de tip "moved" cu dest_path
        nu un CreatedEvent. Fara acest handler, astfel de fisiere ar fi invizibile
        """
        if not isinstance(event, FileMovedEvent) or event.is_directory:
            return
        
        dest_path = os.fsdecode(event.dest_path)
        if self._is_in_monitored_directory(dest_path):
            self._handle_file_event(dest_path, "file_created")

    def _handle_file_event(
        self,
        file_path: str,
        event_type: str,
    ) -> None:
        
        """Filtrează și raportează un eveniment relevant de fișier."""

        normalized_path = normalize_file_path(file_path)
        if not self._is_relevant_file(normalized_path):
            self.logger.debug(
                "Ignored %s event for file with unmonitored extension: %s",
                event_type,
                normalized_path,
            )
            return
        


        if self.debouncer.is_duplicate(event_type, normalized_path):
            self.logger.debug(
                "Ignored duplicate %s event for file: %s",
                event_type,
                normalized_path,
            )
            return

        payload = build_file_event_payload(
            agent_id=self.agent_id,
            agent_instance_id=self.agent_instance_id,
            event_type=event_type,
            file_path=normalized_path,
        )

        try:
            self.event_callback(payload)
            self.logger.info(
                "Detected and reported %s event for file: %s",
                event_type,
                normalized_path,
            )
        except Exception as error:
            # Monitorizarea nu trebuie să se oprească doar pentru că raportarea
            # unui eveniment a eșuat temporar.
            self.logger.warning(
                "Could not report %s event for file %s: %s",
                event_type,
                normalized_path,
                error,
            )


class FileMonitor:
    """
    Gestionează monitorizarea configurabilă a mai multor directoare.

    Monitorizarea poate fi recursivă și funcționează prin biblioteca watchdog,
    compatibilă cu Windows, Linux și macOS.
    """

    def __init__(
        self,
        agent_id: str,
        agent_instance_id: str,
        monitored_directories: Iterable[str],
        recursive_monitoring: bool,
        event_callback: FileEventCallback,
        logger: Optional[logging.Logger] = None,
        monitored_extensions: FrozenSet[str] = DEFAULT_MONITORED_EXTENSIONS,
        debounce_seconds: float = DEFAULT_EVENT_DEBOUNCE_SECONDS,
    ):
        self.agent_id = agent_id
        self.agent_instance_id = agent_instance_id
        self.monitored_directories = list(monitored_directories)
        self.recursive_monitoring = recursive_monitoring
        self.logger = logger or logging.getLogger(__name__)

        self.observer = Observer()
        self.handler = EDRFileEventHandler(
            agent_id=agent_id,
            agent_instance_id=agent_instance_id,
            monitored_directories=self.monitored_directories,
            event_callback=event_callback,
            logger=self.logger,
            monitored_extensions=monitored_extensions,
            debounce_seconds=debounce_seconds,
        )

        self._started = False

    def start(self) -> None:
        """
        Pornește monitorizarea pentru toate directoarele valide configurate.

        Directoarele inexistente sunt ignorate și raportate în log.
        Agentul nu creează automat directoare arbitrare din configurație.
        """

        if self._started:
            self.logger.warning("File monitoring is already running.")
            return

        valid_directories_count = 0

        for directory in self.monitored_directories:
            directory_path = Path(directory)

            if not directory_path.exists():
                self.logger.warning(
                    "Monitored directory does not exist and will be skipped: %s",
                    directory_path,
                )
                continue

            if not directory_path.is_dir():
                self.logger.warning(
                    "Configured monitored path is not a directory and will be skipped: %s",
                    directory_path,
                )
                continue

            self.observer.schedule(
                self.handler,
                str(directory_path),
                recursive=self.recursive_monitoring,
            )

            valid_directories_count += 1

            self.logger.info(
                "Scheduled directory monitoring: %s | recursive=%s",
                directory_path,
                self.recursive_monitoring,
            )

        if valid_directories_count == 0:
            raise FileMonitorError(
                "File monitoring could not start because no valid directories were found"
            )

        self.observer.start()
        self._started = True

        self.logger.info(
            "File monitoring started successfully for %s directorie(s).",
            valid_directories_count,
        )

    def stop(self) -> None:
        """Oprește monitorizarea directoarelor."""
        if self._started:
            self.logger.info("Stopping file monitoring...")
            self.observer.stop()

    def join(self, timeout: Optional[float] = None) -> None:
        """Așteaptă oprirea completă a thread-ului watchdog."""
        if self._started:
            self.observer.join(timeout=timeout)

    def is_running(self) -> bool:
        """Returnează dacă observer-ul de monitorizare este activ."""
        return self._started and self.observer.is_alive()