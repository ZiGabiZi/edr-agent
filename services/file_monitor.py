import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple
from uuid import uuid4

from watchdog.events import FileMovedEvent, FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from services.settle_tracker import (
    DEFAULT_MAX_SETTLE_WAIT_SECONDS,
    DEFAULT_SETTLE_QUIET_SECONDS,
    PendingFile,
    SettleTracker,
)


DEFAULT_MONITORED_EXTENSIONS: FrozenSet[str] = frozenset()

# Cât de des verifică firul de eliberare dacă tracker-ul are fișiere gata de
# raportat. Valoarea adaugă cel mult atâta latență peste perioada de liniște,
# deci trebuie să fie mică față de quiet_seconds, nu față de ceva absolut.
DEFAULT_RELEASE_POLL_SECONDS = 0.25

# De câte ori se încearcă livrarea unui eveniment către spool înainte de a
# renunța. Vezi SettleReleaser pentru motivul existenței reîncercărilor.
_RELEASER_MAX_EMIT_ATTEMPTS = 5

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


class EDRFileEventHandler(FileSystemEventHandler):
    """
    Filtrează evenimentele watchdog și le predă tracker-ului de stabilizare.

    Handler-ul nu mai construiește payload-uri și nu mai apelează callback-ul
    de raportare: rulează pe firul observer-ului watchdog, unde orice lucru
    care poate dura sau eșua este interzis. Singura lui treabă este să decidă
    dacă evenimentul este relevant și, dacă da, să-l observe în tracker —
    operație de memorie, fără I/O, care revine imediat.

    Emiterea efectivă aparține lui SettleReleaser, pe firul lui propriu.
    """

    def __init__(
        self,
        monitored_directories: Iterable[str],
        tracker: SettleTracker,
        logger: logging.Logger,
        monitored_extensions: FrozenSet[str] = DEFAULT_MONITORED_EXTENSIONS,
    ):
        super().__init__()

        self.monitored_directories = tuple(
            normalize_file_path(directory)
            for directory in monitored_directories
            )
        self.tracker = tracker
        self.logger = logger
        self.monitored_extensions = monitored_extensions


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
        
        """Filtrează un eveniment de fișier și îl predă tracker-ului."""

        normalized_path = normalize_file_path(file_path)
        if not self._is_relevant_file(normalized_path):
            self.logger.debug(
                "Ignored %s event for file with unmonitored extension: %s",
                event_type,
                normalized_path,
            )
            return

        try:
            self.tracker.observe(normalized_path, event_type)
        except Exception as error:
            # observe() nu face I/O, deci în practică nu are cum să eșueze.
            # Garda rămâne totuși: o excepție scăpată aici omoară firul
            # observer-ului watchdog, iar monitorizarea moare fără niciun semn
            # exterior. Prețul unei gărzi inutile este zero; prețul absenței ei
            # este orbire tăcută.
            self.logger.warning(
                "Could not track %s event for file %s: %s",
                event_type,
                normalized_path,
                error,
            )


class SettleReleaser:
    """
    Golește periodic tracker-ul și emite evenimentele devenite gata.

    Rulează pe un fir propriu, deținut de FileMonitor. Bucla consultă
    SettleTracker.due() la fiecare poll_seconds, construiește payload-ul
    fiecărui fișier eliberat și îl predă callback-ului de raportare (în
    producție: spool-ul de evenimente).

    De ce există reîncercările — și de ce nu existau înainte
    ------------------------------------------------------
    În vechiul flux, callback-ul era apelat direct de pe firul watchdog, iar un
    eșec era recuperabil de la sine: următoarea scriere în fișier regenera
    evenimentul. due() însă SCOATE intrarea din tracker; fișierul s-a liniștit,
    deci niciun eveniment watchdog nu-l va mai reînvia. Același eșec de
    callback, dintr-o pierdere temporară, devine pierdere definitivă. De aceea
    un payload care nu a putut fi predat se reține și se reîncearcă la
    trecerile următoare.

    Payload-ul se construiește O SINGURĂ DATĂ, la prima încercare, și se
    reține ca atare între reîncercări: client_event_id rămâne astfel stabil,
    iar deduplicarea de pe server (semantica at-least-once a spool-ului)
    rămâne validă chiar dacă o încercare a apucat să producă efecte parțiale.

    După max_emit_attempts eșecuri consecutive, evenimentul se abandonează cu
    un log de nivel ERROR (Decizia 2): un disc plin sau un spool corupt nu
    trebuie să blocheze coada la nesfârșit, dar nici să treacă neobservat.

    Starea de reîncercare (_retry) este atinsă exclusiv de pe firul releaser-ului
    (bucla run() și drenarea de la oprire rulează pe același fir), deci nu are
    nevoie de lacăt. Testele apelează release_due_once() direct, fără fir.
    """

    def __init__(
        self,
        tracker: SettleTracker,
        event_callback: FileEventCallback,
        agent_id: str,
        agent_instance_id: str,
        logger: logging.Logger,
        poll_seconds: float = DEFAULT_RELEASE_POLL_SECONDS,
        max_emit_attempts: int = _RELEASER_MAX_EMIT_ATTEMPTS,
    ):
        self.tracker = tracker
        self.event_callback = event_callback
        self.agent_id = agent_id
        self.agent_instance_id = agent_instance_id
        self.logger = logger
        self.poll_seconds = poll_seconds
        self.max_emit_attempts = max_emit_attempts

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Payload-uri care au eșuat la livrare, cu numărul de încercări
        # consumate. Listă simplă, fără lacăt — vezi docstring-ul clasei.
        self._retry: List[Tuple[Dict[str, Any], int]] = []

    def start(self) -> None:
        """Pornește firul de eliberare."""
        if self._thread is not None:
            self.logger.warning("Settle releaser is already running.")
            return

        self._thread = threading.Thread(
            target=self.run,
            name="settle-releaser",
            daemon=True,
        )
        self._thread.start()

    def run(self) -> None:
        """
        Bucla firului: o trecere la fiecare poll_seconds, până la semnalul de
        oprire, apoi drenarea finală.

        Fiecare trecere este împachetată într-o gardă: garanția tracker-ului
        („orice intrare iese în cel mult max_wait_seconds") este adevărată doar
        cât timp cineva apelează due(). Dacă firul ar muri dintr-o excepție
        neprevăzută, tracker-ul s-ar umple în tăcere și monitorizarea ar orbi
        fără niciun semn exterior — un mod de eșec pe care vechiul flux, rulând
        pe firul watchdog, nu îl avea.
        """
        while not self._stop_event.wait(self.poll_seconds):
            try:
                self.release_due_once()
            except Exception:
                self.logger.exception(
                    "Settle release pass failed; the loop continues."
                )

        try:
            self.drain_for_shutdown()
        except Exception:
            self.logger.exception("Settle shutdown drain failed.")

    def stop(self) -> None:
        """
        Semnalează oprirea. Drenarea finală rulează pe firul releaser-ului,
        după ieșirea din buclă — apelantul trebuie să cheme apoi join().

        Ordinea corectă aparține lui FileMonitor.join(): observer-ul watchdog
        trebuie oprit ȘI așteptat înainte de acest apel, altfel evenimente
        sosite după drenare ar intra într-un tracker pe care nimeni nu-l mai
        golește.
        """
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        """Așteaptă terminarea firului, inclusiv drenarea finală."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def release_due_once(self) -> int:
        """
        Corpul unei treceri: întâi reîncercările, apoi ce a devenit gata.

        Reîncercările au prioritate pentru că au așteptat deja cel puțin o
        trecere întreagă. Metoda este publică și apelabilă direct din teste,
        cu ceas fals în tracker — firul nu este necesar pentru corectitudine,
        doar pentru cadență.

        Returnează numărul de evenimente predate cu succes.
        """
        emitted = 0

        pending_retries = self._retry
        self._retry = []
        for payload, attempts in pending_retries:
            emitted += self._emit(payload, attempts)

        for pending in self.tracker.due():
            emitted += self._emit(self._build_payload(pending), 0)

        return emitted

    def drain_for_shutdown(self) -> int:
        """
        Ultima acțiune a firului: golește tot ce a rămas, cu o singură șansă.

        La oprire nu mai există „treceri următoare", deci semantica
        reîncercărilor nu mai are sens: fiecare payload — reîncercare veche sau
        intrare proaspăt eliberată de flush() — primește exact o încercare,
        iar eșecul se raportează direct la nivel ERROR. Tehnic, asta înseamnă
        a intra în _emit cu contorul deja la max_emit_attempts - 1.
        """
        emitted = 0

        pending_retries = self._retry
        self._retry = []
        for payload, _ in pending_retries:
            emitted += self._emit(payload, self.max_emit_attempts - 1)

        for pending in self.tracker.flush():
            emitted += self._emit(
                self._build_payload(pending), self.max_emit_attempts - 1
            )

        return emitted

    def _build_payload(self, pending: PendingFile) -> Dict[str, Any]:
        return build_file_event_payload(
            agent_id=self.agent_id,
            event_type=pending.event_type,
            file_path=pending.path,
            agent_instance_id=self.agent_instance_id,
            occurred_at=pending.occurred_at,
            settle_wait_ms=pending.settle_wait_ms,
        )

    def _emit(self, payload: Dict[str, Any], attempts_so_far: int) -> int:
        """
        O încercare de livrare. La eșec, reține payload-ul pentru trecerea
        următoare sau, la epuizarea încercărilor, îl abandonează cu ERROR.
        """
        try:
            self.event_callback(payload)
        except Exception as error:
            attempts = attempts_so_far + 1

            if attempts >= self.max_emit_attempts:
                self.logger.error(
                    "Dropping %s event for file %s after %d failed delivery "
                    "attempts: %s",
                    payload.get("event_type"),
                    payload.get("file_path"),
                    attempts,
                    error,
                )
            else:
                self._retry.append((payload, attempts))
                self.logger.warning(
                    "Could not report %s event for file %s "
                    "(attempt %d of %d), will retry: %s",
                    payload.get("event_type"),
                    payload.get("file_path"),
                    attempts,
                    self.max_emit_attempts,
                    error,
                )
            return 0

        self.logger.info(
            "Detected and reported %s event for file: %s",
            payload.get("event_type"),
            payload.get("file_path"),
        )
        return 1


class FileMonitor:
    """
    Gestionează monitorizarea configurabilă a mai multor directoare.

    Monitorizarea poate fi recursivă și funcționează prin biblioteca watchdog,
    compatibilă cu Windows, Linux și macOS.

    Deține trei piese și le leagă:
      - observer-ul watchdog, care produce observații brute;
      - SettleTracker, care decide când un fișier a încetat să se schimbe;
      - SettleReleaser, pe fir propriu, care emite ce a devenit gata.
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
        quiet_seconds: float = DEFAULT_SETTLE_QUIET_SECONDS,
        max_wait_seconds: float = DEFAULT_MAX_SETTLE_WAIT_SECONDS,
        release_poll_seconds: float = DEFAULT_RELEASE_POLL_SECONDS,
    ):
        self.agent_id = agent_id
        self.agent_instance_id = agent_instance_id
        self.monitored_directories = list(monitored_directories)
        self.recursive_monitoring = recursive_monitoring
        self.logger = logger or logging.getLogger(__name__)

        self.tracker = SettleTracker(
            quiet_seconds=quiet_seconds,
            max_wait_seconds=max_wait_seconds,
            logger=self.logger,
        )

        self.releaser = SettleReleaser(
            tracker=self.tracker,
            event_callback=event_callback,
            agent_id=agent_id,
            agent_instance_id=agent_instance_id,
            logger=self.logger,
            poll_seconds=release_poll_seconds,
        )

        self.observer = Observer()
        self.handler = EDRFileEventHandler(
            monitored_directories=self.monitored_directories,
            tracker=self.tracker,
            logger=self.logger,
            monitored_extensions=monitored_extensions,
        )

        self._started = False

    def start(self) -> None:
        """
        Pornește monitorizarea pentru toate directoarele valide configurate.

        Directoarele inexistente sunt ignorate și raportate în log.
        Agentul nu creează automat directoare arbitrare din configurație.

        Firul de eliberare pornește doar dacă observer-ul a pornit: fără
        observații nu are ce elibera, iar un fir orfan care se rotește peste un
        tracker mereu gol ar fi doar zgomot.
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
        self.releaser.start()
        self._started = True

        self.logger.info(
            "File monitoring started successfully for %s directorie(s).",
            valid_directories_count,
        )

    def stop(self) -> None:
        """
        Oprește observer-ul watchdog. Deliberat, NU oprește releaser-ul.

        Ordinea la oprire nu este negociabilă: releaser-ul trebuie semnalizat
        abia după ce observer-ul a fost oprit ȘI așteptat (în join()), altfel
        observații sosite între drenarea finală și moartea observer-ului ar
        intra într-un tracker pe care nimeni nu-l mai golește — pierdute fără
        nicio urmă. Secvența completă trăiește în join().
        """
        if self._started:
            self.logger.info("Stopping file monitoring...")
            self.observer.stop()

    def join(self, timeout: Optional[float] = None) -> None:
        """
        Așteaptă oprirea completă, în ordinea care nu pierde evenimente:

          1. observer-ul watchdog se termină — nu mai pot sosi observații;
          2. releaser-ul primește semnalul de oprire, iar ultima lui acțiune,
             pe firul lui, este drenarea: tracker.flush() plus emiterea a tot
             ce a ieșit;
          3. se așteaptă firul releaser-ului.

        Abia la final _started devine fals: monitorul este „pornit" până când
        ultima lui piesă s-a oprit de tot, nu până când prima a primit semnalul.
        """
        if not self._started:
            return

        self.observer.join(timeout=timeout)
        self.releaser.stop()
        self.releaser.join(timeout=timeout)
        self._started = False

    def is_running(self) -> bool:
        """Returnează dacă observer-ul de monitorizare este activ."""
        return self._started and self.observer.is_alive()