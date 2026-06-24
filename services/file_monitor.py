import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, Iterable, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


DEFAULT_EVENT_DEBOUNCE_SECONDS = 2.0
_DEBOUNCE_CLEANUP_THRESHOLD = 500
_DEBOUNCE_CLEANUP_INTERVAL_SECONDS = 60.0

FileEventCallback = Callable[[Dict[str, str]], None]


class FileMonitorError(Exception):
    """Eroare ridicată atunci când monitorizarea directoarelor nu poate porni."""
    pass


def normalize_file_path(file_path: str) -> str:
    """
    Normalizează calea unui fișier pentru raportare și comparare.

    Funcționează atât pe Windows, cât și pe Linux.
    """
    return os.path.normpath(os.path.abspath(file_path))


def build_file_event_payload(
    agent_id: str,
    event_type: str,
    file_path: str,
) -> Dict[str, str]:
    """Construiește payload-ul unui eveniment de fișier."""
    current_time = datetime.now(timezone.utc).isoformat()
    normalized_path = normalize_file_path(file_path)

    descriptions = {
        "file_created": "New file detected in monitored directory",
        "file_modified": "File modified in monitored directory",
    }

    description = descriptions.get(event_type, "File system event detected")

    return {
        "agent_id": agent_id,
        "event_type": event_type,
        "file_path": normalized_path,
        "description": f"{description} at {current_time}",
    }


class EventDebouncer:
    """
    Reduce raportarea repetată a aceluiași eveniment într-un interval scurt.

    Unele aplicații și unele sisteme de operare pot genera mai multe evenimente
    pentru aceeași operație de scriere a unui fișier.
    """

    def __init__(self, interval_seconds: float = DEFAULT_EVENT_DEBOUNCE_SECONDS):
        self.interval_seconds = interval_seconds
        self._last_seen: Dict[str, float] = {}
        self._last_cleanup_time = 0.0
        self._lock = Lock()

    def _cleanup_stale_entries(self, current_time: float) -> None:
        """Curăță intrările vechi din dicționarul de evenimente văzute recent.
           Această metodă este apelată periodic pentru a preveni creșterea necontrolată a memoriei.
        """
        if len(self._last_seen) < _DEBOUNCE_CLEANUP_THRESHOLD:
            return
        
        if (current_time - self._last_cleanup_time) < _DEBOUNCE_CLEANUP_INTERVAL_SECONDS:
            return
        
        cutoff = current_time - self.interval_seconds * 2

        self._last_seen = {
            key: timestamp
            for key, timestamp in self._last_seen.items()
            if timestamp > cutoff
        }
        self._last_cleanup_time = current_time

    def is_duplicate(self, event_type: str, file_path: str) -> bool:
        """Returnează True dacă evenimentul a fost observat recent."""
        event_key = f"{event_type}:{os.path.normcase(normalize_file_path(file_path))}"
        current_time = time.monotonic()

        with self._lock:
            self._cleanup_stale_entries(current_time)
            previous_time = self._last_seen.get(event_key)
            self._last_seen[event_key] = current_time

        if previous_time is None:
            return False

        return (current_time - previous_time) < self.interval_seconds


class EDRFileEventHandler(FileSystemEventHandler):
    """Procesează evenimentele de fișier detectate de watchdog."""

    def __init__(
        self,
        agent_id: str,
        event_callback: FileEventCallback,
        logger: logging.Logger,
        debounce_seconds: float = DEFAULT_EVENT_DEBOUNCE_SECONDS,
    ):
        super().__init__()

        self.agent_id = agent_id
        self.event_callback = event_callback
        self.logger = logger
        self.debouncer = EventDebouncer(debounce_seconds)

    def on_created(self, event: FileSystemEvent) -> None:
        """Procesează apariția unui fișier nou."""
        self._handle_file_event(event, "file_created")

    def on_modified(self, event: FileSystemEvent) -> None:
        """Procesează modificarea unui fișier existent."""
        self._handle_file_event(event, "file_modified")

    def _handle_file_event(
        self,
        event: FileSystemEvent,
        event_type: str,
    ) -> None:
        """Filtrează și raportează un eveniment relevant de fișier."""
        if event.is_directory:
            return

        file_path = normalize_file_path(event.src_path)

        if self.debouncer.is_duplicate(event_type, file_path):
            self.logger.debug(
                "Ignored duplicate %s event for file: %s",
                event_type,
                file_path,
            )
            return

        payload = build_file_event_payload(
            agent_id=self.agent_id,
            event_type=event_type,
            file_path=file_path,
        )

        try:
            self.event_callback(payload)
            self.logger.info(
                "Detected and reported %s event for file: %s",
                event_type,
                file_path,
            )
        except Exception as error:
            # Monitorizarea nu trebuie să se oprească doar pentru că raportarea
            # unui eveniment a eșuat temporar.
            self.logger.warning(
                "Could not report %s event for file %s: %s",
                event_type,
                file_path,
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
        monitored_directories: Iterable[str],
        recursive_monitoring: bool,
        event_callback: FileEventCallback,
        logger: Optional[logging.Logger] = None,
        debounce_seconds: float = DEFAULT_EVENT_DEBOUNCE_SECONDS,
    ):
        self.agent_id = agent_id
        self.monitored_directories = list(monitored_directories)
        self.recursive_monitoring = recursive_monitoring
        self.logger = logger or logging.getLogger(__name__)

        self.observer = Observer()
        self.handler = EDRFileEventHandler(
            agent_id=agent_id,
            event_callback=event_callback,
            logger=self.logger,
            debounce_seconds=debounce_seconds,
        )

        self._started = False

    def start(self) -> None:
        """
        Pornește monitorizarea pentru toate directoarele valide configurate.

        Directoarele inexistente sunt ignorate și raportate în log.
        Agentul nu creează automat directoare arbitrare din configurație.
        """
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