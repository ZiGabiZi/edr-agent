import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, FrozenSet, Iterable, Optional

from watchdog.events import FileMovedEvent, FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from services.file_event import (  # noqa: F401  (reexportate pentru apelanții existenți)
    FileEventCallback,
    build_file_event_payload,
    normalize_file_path,
)
from services.file_hasher import (
    DEFAULT_SHUTDOWN_HASH_BUDGET_SECONDS,
    MAX_HASH_QUEUE_DEPTH,
    MAX_HASH_REINTRODUCTIONS,
    MAX_HASHABLE_FILE_SIZE_BYTES,
    FileHasher,
    HashSubmitter,
)
from services.settle_tracker import (
    DEFAULT_MAX_SETTLE_WAIT_SECONDS,
    DEFAULT_SETTLE_QUIET_SECONDS,
    SettleTracker,
)


DEFAULT_MONITORED_EXTENSIONS: FrozenSet[str] = frozenset()

# Cât de des verifică firul de eliberare dacă tracker-ul are fișiere gata de
# raportat. Valoarea adaugă cel mult atâta latență peste perioada de liniște,
# deci trebuie să fie mică față de quiet_seconds, nu față de ceva absolut.
DEFAULT_RELEASE_POLL_SECONDS = 0.25

# Cât din bugetul TOTAL de oprire se rezervă pentru raportare, după ce
# hashing-ul s-a oprit.
#
# Fără rezerva asta, termenul de hashing ar coincide cu termenul lui join(),
# iar drenarea ar mai avea de emis exact atunci când apelantul renunță să mai
# aștepte — adică fix scenariul în care agentul închide spool-ul sub un fir
# încă viu, iar coada întreagă se pierde câte un ERROR pe rând.
#
# O secundă acoperă confortabil MAX_HASH_QUEUE_DEPTH scrieri în spool.
DEFAULT_SHUTDOWN_REPORT_RESERVE_SECONDS = 1.0


class FileMonitorError(Exception):
    """Eroare ridicată atunci când monitorizarea directoarelor nu poate porni."""
    pass


class EDRFileEventHandler(FileSystemEventHandler):
    """
    Filtrează evenimentele watchdog și le predă tracker-ului de stabilizare.

    Handler-ul nu construiește payload-uri și nu apelează callback-ul de
    raportare: rulează pe firul observer-ului watchdog, unde orice lucru care
    poate dura sau eșua este interzis. Singura lui treabă este să decidă dacă
    evenimentul este relevant și, dacă da, să-l observe în tracker — operație
    de memorie, fără I/O, care revine imediat.
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
    Mută fișierele stabilizate din tracker în coada de hashing.

    Rulează pe un fir propriu, deținut de FileMonitor. Nu construiește
    payload-uri și nu emite nimic — de la introducerea hasher-ului, raportarea
    aparține exclusiv acestuia, pentru că doar acolo se știe ce hash_status
    poartă evenimentul.

    Firul rămâne separat de cel al hasher-ului tocmai pentru că hashing-ul
    poate dura: dacă aceeași buclă ar face și golirea tracker-ului, un fișier
    de 200 MB ar bloca due() pentru toată durata citirii, iar garanția
    tracker-ului („orice intrare iese în cel mult max_wait_seconds") ar cădea.
    Aici, fiecare trecere e în timp constant.
    """

    def __init__(
        self,
        tracker: SettleTracker,
        hasher: HashSubmitter,
        logger: logging.Logger,
        poll_seconds: float = DEFAULT_RELEASE_POLL_SECONDS,
    ):
        self.tracker = tracker
        self.hasher = hasher
        self.logger = logger
        self.poll_seconds = poll_seconds

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

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
        este adevărată doar cât timp cineva apelează due(). Dacă firul ar muri
        dintr-o excepție neprevăzută, tracker-ul s-ar umple în tăcere și
        monitorizarea ar orbi fără niciun semn exterior — un mod de eșec pe
        care vechiul flux, rulând pe firul watchdog, nu îl avea.
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

    def is_alive(self) -> bool:
        """Dacă firul de eliberare mai rulează (inclusiv în drenare)."""
        return self._thread is not None and self._thread.is_alive()

    def release_due_once(self) -> int:
        """
        Corpul unei treceri: tot ce s-a stabilizat trece în coada de hashing.

        Metoda este publică și apelabilă direct din teste, cu ceas fals în
        tracker — firul nu este necesar pentru corectitudine, doar pentru
        cadență.

        Returnează numărul de fișiere predate hasher-ului.
        """
        handed_over = 0

        for pending in self.tracker.due():
            self.hasher.submit(pending)
            handed_over += 1

        return handed_over

    def drain_for_shutdown(self) -> int:
        """
        Ultima acțiune a firului: tot ce mai aștepta stabilizarea trece la
        hashing, ca hasher-ul să îl găsească în coadă când ajunge și el la
        drenare.
        """
        handed_over = 0

        for pending in self.tracker.flush():
            self.hasher.submit(pending)
            handed_over += 1

        return handed_over


class FileMonitor:
    """
    Gestionează monitorizarea configurabilă a mai multor directoare.

    Monitorizarea poate fi recursivă și funcționează prin biblioteca watchdog,
    compatibilă cu Windows, Linux și macOS.

    Deține patru piese și le leagă într-un lanț cu trei etaje de fire:

        watchdog  -> tracker.observe()            (fir observer, timp constant)
        releaser  -> tracker.due() -> hasher      (fir propriu, timp constant)
        hasher    -> stat/hash/re-stat -> spool   (fir propriu, poate dura)

    Separarea nu e ornamentală: fiecare etaj protejează garanția etajului de
    dinaintea lui de latența etajului de după.
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
        report_reserve_seconds: float = DEFAULT_SHUTDOWN_REPORT_RESERVE_SECONDS,
        # Reglajele hasher-ului. Trec prin monitor pentru că el construiește
        # FileHasher; valorile implicite rămân cele din services/file_hasher.py,
        # unde stă și raționamentul care le-a ales. Un apelant care nu le pasează
        # nu trebuie să le cunoască.
        max_file_size_bytes: int = MAX_HASHABLE_FILE_SIZE_BYTES,
        max_queue_depth: int = MAX_HASH_QUEUE_DEPTH,
        max_reintroductions: int = MAX_HASH_REINTRODUCTIONS,
        shutdown_budget_seconds: float = DEFAULT_SHUTDOWN_HASH_BUDGET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.report_reserve_seconds = report_reserve_seconds
        self._clock = clock
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

        self.hasher = FileHasher(
            tracker=self.tracker,
            event_callback=event_callback,
            agent_id=agent_id,
            agent_instance_id=agent_instance_id,
            logger=self.logger,
            max_file_size_bytes=max_file_size_bytes,
            max_queue_depth=max_queue_depth,
            max_reintroductions=max_reintroductions,
            shutdown_budget_seconds=shutdown_budget_seconds,
        )

        self.releaser = SettleReleaser(
            tracker=self.tracker,
            hasher=self.hasher,
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

        Firele auxiliare pornesc doar dacă observer-ul a pornit: fără
        observații nu au ce prelucra, iar fire orfane care se rotesc peste cozi
        mereu goale ar fi doar zgomot.
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
        self.hasher.start()
        self.releaser.start()
        self._started = True

        self.logger.info(
            "File monitoring started successfully for %s directorie(s).",
            valid_directories_count,
        )

    def stop(self) -> None:
        """
        Oprește observer-ul watchdog. Deliberat, NU oprește celelalte fire.

        Ordinea la oprire nu este negociabilă și trăiește în join(): fiecare
        etaj trebuie drenat complet înainte ca următorul să fie semnalizat,
        altfel drenarea unuia produce intrări pe care următorul nu le mai
        colectează niciodată.
        """
        if self._started:
            self.logger.info("Stopping file monitoring...")
            self.observer.stop()

    def join(self, timeout: Optional[float] = None) -> bool:
        """
        Așteaptă oprirea completă, în ordinea care nu pierde evenimente:

          1. observer-ul watchdog se termină — nu mai pot sosi observații;
          2. releaser-ul primește semnalul, iar ultima lui acțiune, pe firul
             lui, este tracker.flush() cu predarea tuturor intrărilor rămase
             către coada hasher-ului;
          3. se așteaptă firul releaser-ului — abia acum coada hasher-ului e
             completă;
          4. hasher-ul primește semnalul ȘI termenul, apoi își golește coada:
             hash-uiește cât încape până la termen, raportează restul fără hash;
          5. se așteaptă firul hasher-ului.

        Inversarea pașilor 3 și 4 ar fi cea mai ușoară greșeală de făcut aici:
        hasher-ul ar drena o coadă în care flush-ul releaser-ului nu a ajuns
        încă, iar acele fișiere n-ar fi raportate niciodată.

        timeout este un buget TOTAL, nu unul per etaj
        --------------------------------------------
        Înainte, același timeout se dădea fiecăreia dintre cele trei așteptări,
        deci join(timeout=5) putea dura 15 secunde. Semnătura promitea o limită
        și livra un multiplu al ei — minciună care creștea tăcut cu fiecare etaj
        adăugat. Aici se calculează un singur termen absolut la intrare, iar
        fiecare etaj primește cât a mai rămas din el.

        Termenul de HASHING este cu report_reserve_seconds mai devreme decât
        termenul total, ca drenarea să apuce să-și emită coada înainte ca
        apelantul să renunțe. Fără rezerva asta, hashing-ul s-ar opri exact
        când join() cedează, iar raportarea ar cădea în intervalul în care
        agentul deja închide spool-ul.

        Returnează True dacă toate cele trei etaje s-au oprit de tot. False
        înseamnă că un fir e încă viu — informație de care apelantul are nevoie
        înainte să închidă resurse pe care firul acela încă le folosește.
        """
        if not self._started:
            return True

        deadline = None if timeout is None else self._clock() + timeout

        def remaining() -> Optional[float]:
            if deadline is None:
                return None
            return max(0.0, deadline - self._clock())

        self.observer.join(timeout=remaining())

        self.releaser.stop()
        self.releaser.join(timeout=remaining())

        hash_deadline = (
            None if deadline is None else deadline - self.report_reserve_seconds
        )
        self.hasher.stop(deadline=hash_deadline)
        self.hasher.join(timeout=remaining())

        self._started = False

        stalled = [
            name
            for name, alive in (
                ("watchdog observer", self.observer.is_alive()),
                ("settle releaser", self.releaser.is_alive()),
                ("file hasher", self.hasher.is_alive()),
            )
            if alive
        ]

        if stalled:
            self.logger.error(
                "File monitoring did not stop within its budget; still running: "
                "%s. These threads may still be writing to the event spool.",
                ", ".join(stalled),
            )
            return False

        return True

    def is_running(self) -> bool:
        """Returnează dacă observer-ul de monitorizare este activ."""
        return self._started and self.observer.is_alive()