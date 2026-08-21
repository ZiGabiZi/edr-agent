"""
Calculul SHA-256 pe endpoint: treapta T0 a protocolului de divulgare progresivă.
===============================================================================

Aici se produce, pentru prima dată, identitatea de conținut pe care serverul o
poate interoga într-o reputație fără ca fișierul să părăsească endpoint-ul.
Până la acest modul, agentul raporta doar că un fișier a apărut; de acum
raportează CE anume a apărut, în 32 de octeți în loc de tot fișierul.

De ce hashing-ul are un fir propriu
-----------------------------------
Citirea integrală a unui fișier poate dura zeci de secunde. Pe firul
releaser-ului, acel timp ar însemna tot atâtea secunde în care SettleTracker.due()
nu se apelează — iar garanția tracker-ului („orice intrare iese în cel mult
max_wait_seconds") este adevărată doar cât timp cineva îl golește. Hashing-ul pe
firul releaser-ului ar transforma un fișier mare în orbire temporară a
monitorizării.

De ce hash-ul singur nu e de ajuns: verificarea dublă
----------------------------------------------------
SettleTracker produce un CANDIDAT, nu un adevăr — watchdog nu garantează un
eveniment pentru fiecare scriere, deci „nicio observație de o secundă" înseamnă
„nu mi s-a spus că cineva a scris", nu „nimeni n-a scris". Un hash calculat pe
un fișier încă în scriere descrie o stare intermediară care nu a existat
niciodată ca fișier finit. Serverul l-ar interoga în reputație, ar primi
„necunoscut" și ar escalada — zgomot în cel mai bun caz, iar în cel mai rău un
fișier malițios cunoscut trecut drept necunoscut fiindcă i-am citit jumătate.

Apărarea: stat înainte, hash, stat după. Dacă dimensiunea sau mtime s-au
schimbat, rezultatul se aruncă și fișierul se reintroduce în așteptare.

Ce NU garantează, și merită spus limpede: un fișier care revine exact la aceeași
dimensiune cu mtime intact, în intervalul dintre cele două citiri, trece
nedetectat. Fereastra nu se închide fără suport de kernel. Garanția reală e mai
modestă, dar utilă: dacă un hash e raportat, fișierul nu s-a schimbat OBSERVABIL
în timpul citirii.
"""

import hashlib
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Protocol, Tuple

from services.file_event import FileEventCallback, build_file_event_payload
from services.settle_tracker import PendingFile, SettleTracker


# De câte ori un fișier poate fi repus în așteptare după ce verificarea dublă
# a arătat că s-a schimbat în timpul citirii. La epuizare, se raportează
# hash_status='unstable' fără sha256.
#
# Valoare de lucru, aleasă pe raționament, nu calibrată pe măsurători: un
# fișier scris o singură dată are nevoie de cel mult o reintroducere, iar un
# fișier care se scrie continuu epuizează orice prag rezonabil oricum. Marja
# până la 3 acoperă cazul intermediar — fișierul activ, dar nu perpetuu.
MAX_HASH_REINTRODUCTIONS = 3

# Peste această dimensiune fișierul nu se citește deloc: se raportează
# hash_status='too_large' cu file_size populat din stat().
#
# Nu e o judecată de securitate — fișierele mari nu sunt mai sigure. E o
# decizie de cost: citirea integrală a câtorva gigaocteți e un consum mare de
# I/O pentru o probabilitate mică de regăsire într-o bază de reputație, unde
# malware-ul real e aproape întotdeauna mic. E primul loc din agent unde
# principiul divulgării progresive se aplică nu octeților trimiși pe rețea, ci
# timpului de calcul local.
#
# Valoare de lucru; se revizuiește după popularea bazei cu sample-uri reale,
# când distribuția dimensiunilor devine cunoscută.
MAX_HASHABLE_FILE_SIZE_BYTES = 200 * 1024 * 1024

# Câte fișiere pot aștepta hashing simultan.
#
# Mult mai mic decât plafonul tracker-ului (10.000): acolo o intrare costă
# memorie, aici costă un I/O întreg. O rafală obișnuită (dezarhivare, copiere
# de folder) nu îl atinge; dacă îl atinge, chiar înseamnă ceva.
MAX_HASH_QUEUE_DEPTH = 500

# Cât timp mai are voie hasher-ul să CALCULEZE după semnalul de oprire, când
# apelantul nu impune el un termen (FileMonitor.join(timeout=None), teste).
#
# Bugetul mărginește hashing-ul, nu raportarea — distincția e esențială și era
# absentă din prima versiune. Hashing-ul e munca scumpă și opțională: un fișier
# nehash-uit se raportează 'skipped_shutdown' și nu se pierde nimic în afară de
# identitatea lui de conținut. Raportarea e munca ieftină și obligatorie: o
# intrare neraportată dispare fără urmă. A plafona raportarea ar transforma
# presiunea de timp în tăcere, adică exact ce tot mecanismul interzice.
#
# Raportarea nu e nemărginită: e mărginită structural de MAX_HASH_QUEUE_DEPTH
# scrieri în spool, nu de un ceas.
DEFAULT_SHUTDOWN_HASH_BUDGET_SECONDS = 5.0

# Dimensiunea blocului de citire. 1 MiB e un compromis obișnuit între numărul
# de apeluri de sistem și memoria ocupată tranzitoriu.
_HASH_CHUNK_SIZE = 1024 * 1024

_DEFAULT_POLL_SECONDS = 0.1

# Câte încercări de livrare către spool primește un payload înainte de abandon.
_MAX_EMIT_ATTEMPTS = 5


class HashSubmitter(Protocol):
    """
    Tot ce vede SettleReleaser din hasher: o singură metodă.

    Există pentru că releaser-ul nu are nevoie de FileHasher, ci doar de
    capacitatea de a preda un fișier stabilizat. Adnotat cu clasa concretă,
    orice dublură de test ar fi trebuit să moștenească un fir, o coadă și un
    ceas ca să treacă de verificatorul de tipuri — iar testul care contează
    acolo („releaser-ul nu construiește niciodată un payload") s-ar fi sprijinit
    tocmai pe piesa pe care vrea s-o excludă.

    Îngustimea e și documentație: dacă vreodată apare aici o a doua metodă,
    înseamnă că releaser-ul a început să știe despre hashing mai mult decât
    trebuie.
    """

    def submit(self, pending: PendingFile) -> None: ...


class _HashOutcome:
    """Rezultatul unei încercări de hashing. Structură pasivă, fără logică."""

    __slots__ = ("sha256", "hash_status", "file_size", "duration_ms", "changed")

    def __init__(
        self,
        hash_status: str,
        sha256: Optional[str] = None,
        file_size: Optional[int] = None,
        duration_ms: Optional[int] = None,
        changed: bool = False,
    ):
        self.sha256 = sha256
        self.hash_status = hash_status
        self.file_size = file_size
        self.duration_ms = duration_ms
        # True doar când verificarea dublă a prins o schimbare: singurul caz în
        # care fișierul merită reintrodus, nu raportat.
        self.changed = changed


class FileHasher:
    """
    Firul care transformă un fișier stabilizat într-un eveniment cu identitate.

    Primește intrări prin submit() (de pe firul releaser-ului), le prelucrează
    pe firul propriu și predă payload-ul rezultat callback-ului de raportare.

    Emiterea cu reîncercări trăiește aici, mutată din SettleReleaser odată cu
    construcția payload-ului: motivul rămâne cel de la legarea tracker-ului —
    SettleTracker.due() SCOATE intrarea, deci un eșec de livrare nereținut e
    pierdere definitivă, nu temporară.
    """

    def __init__(
        self,
        tracker: SettleTracker,
        event_callback: FileEventCallback,
        agent_id: str,
        agent_instance_id: str,
        logger: logging.Logger,
        max_queue_depth: int = MAX_HASH_QUEUE_DEPTH,
        max_file_size_bytes: int = MAX_HASHABLE_FILE_SIZE_BYTES,
        max_reintroductions: int = MAX_HASH_REINTRODUCTIONS,
        max_emit_attempts: int = _MAX_EMIT_ATTEMPTS,
        shutdown_budget_seconds: float = DEFAULT_SHUTDOWN_HASH_BUDGET_SECONDS,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
        clock=time.monotonic,
    ):
        self.tracker = tracker
        self.event_callback = event_callback
        self.agent_id = agent_id
        self.agent_instance_id = agent_instance_id
        self.logger = logger
        self.max_queue_depth = max_queue_depth
        self.max_file_size_bytes = max_file_size_bytes
        self.max_reintroductions = max_reintroductions
        self.max_emit_attempts = max_emit_attempts
        self.shutdown_budget_seconds = shutdown_budget_seconds
        self.poll_seconds = poll_seconds
        self._clock = clock

        self._queue: Deque[PendingFile] = deque()

        # Intrări degradate la depășirea capacității, care așteaptă doar să fie
        # emise. Există din același motiv ca SettleTracker._ready: submit() e
        # apelat de pe firul releaser-ului, iar acela nu are voie să emită —
        # coada de reîncercări aparține exclusiv firului hasher-ului.
        self._degraded: Deque[PendingFile] = deque()

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Momentul ABSOLUT până la care mai are voie să se calculeze hash-uri.
        # None cât timp agentul rulează normal — atunci nu există termen.
        #
        # Scris de pe firul apelantului (stop()), citit de pe firul hasher-ului
        # la fiecare bloc de 1 MiB. Fără lacăt, deliberat: atribuirea unui
        # atribut e atomică sub GIL, iar singurele valori posibile la citire
        # sunt None (vechea) sau termenul (noua). Ambele sunt sigure — în cel
        # mai rău caz se mai citește un bloc înainte ca termenul să fie văzut.
        # Un lacăt aici ar fi luat și eliberat de milioane de ori pe un fișier
        # mare, ca să apere o cursă al cărei rezultat e 1 MiB de citire în plus.
        self._hash_deadline: Optional[float] = None

        # Atinsă exclusiv de pe firul hasher-ului; fără lacăt, ca la releaser.
        self._retry: List[Tuple[Dict[str, Any], int]] = []

    # ---------------------------------------------------------------- intrare

    def submit(self, pending: PendingFile) -> None:
        """
        Pune un fișier stabilizat la coadă. Apelat de pe firul releaser-ului;
        revine imediat, fără I/O.

        La depășirea adâncimii nu se aruncă nimic: intrarea care NU mai încape
        este degradată pe loc și va fi emisă fără hash, cu
        hash_status='skipped_capacity'. Compromisul rămâne cel pe care
        SettleTracker îl face la plafonul lui — presiunea se convertește în
        raportare degradată, niciodată în tăcere.

        De ce se degradează sosirea, nu capul cozii
        ------------------------------------------
        Prima versiune degrada intrarea cea mai veche, justificat prin analogie
        cu _force_release_over_capacity. Analogia se inversează, și cu ea
        concluzia.

        La SettleTracker, „cea mai veche" înseamnă „cel mai probabil deja
        stabilizată": a o elibera devreme e aproape a o elibera corect, deci e
        alegerea cel mai puțin dăunătoare. Aici, „cea mai veche" înseamnă „cea
        mai apropiată de a fi citită" — _take_next() ia tot din cap. A o degrada
        e alegerea cel mai DĂUNĂTOARE: intrarea a stat la rând tot drumul până
        în față și e refuzată la un pas de hash, adică exact așteptarea pe care
        tocmai a plătit-o e aruncată.

        Numărul de fișiere care primesc hash nu se schimbă — el e dat de
        viteza hasher-ului, nu de politica de coadă. Se schimbă CARE fișiere și
        cât se așteaptă degeaba: degradarea la sosire e imediată și
        previzibilă, iar regula devine una care se poate spune într-o
        propoziție — ce e deja la rând se hash-uiește, ce vine peste se
        raportează pe loc, fără hash.
        """
        with self._lock:
            if len(self._queue) >= self.max_queue_depth:
                self._degraded.append(pending)

                self.logger.warning(
                    "Hash queue is full (%s); reporting %s without a hash.",
                    self.max_queue_depth,
                    pending.path,
                )
                return

            self._queue.append(pending)

    def queue_depth(self) -> int:
        """Câte fișiere așteaptă hashing (fără cele deja degradate)."""
        with self._lock:
            return len(self._queue)

    # ------------------------------------------------------------------- fir

    def start(self) -> None:
        """Pornește firul de hashing."""
        if self._thread is not None:
            self.logger.warning("File hasher is already running.")
            return

        self._thread = threading.Thread(
            target=self.run,
            name="file-hasher",
            daemon=True,
        )
        self._thread.start()

    def run(self) -> None:
        """
        Bucla firului. Fiecare trecere e păzită din același motiv ca la
        releaser: dacă firul moare, coada crește în tăcere și nimeni nu
        raportează nimic despre fișierele din ea.
        """
        while not self._stop_event.wait(self.poll_seconds):
            try:
                self.process_once()
            except Exception:
                self.logger.exception(
                    "Hash pass failed; the loop continues."
                )

        try:
            self.drain_for_shutdown()
        except Exception:
            self.logger.exception("Hash shutdown drain failed.")

    def stop(self, deadline: Optional[float] = None) -> None:
        """
        Semnalează oprirea și fixează termenul de calcul. Drenarea rulează pe
        firul hasher-ului.

        deadline este un MOMENT absolut pe ceasul monoton, nu o durată — și
        vine de la apelant, nu se calculează aici. Diferența e tot ce a fost
        greșit înainte: un buget calculat la intrarea în drenare începe să
        curgă de când firul a apucat să înceapă, iar întârzierea până acolo e
        nemărginită (semnalul de oprire nu se observă cât timp firul e blocat
        într-o citire). Cine cere oprirea știe de când numără; hasher-ul nu.

        Termenul se scrie ÎNAINTE de a semnaliza evenimentul, ca o citire care
        vede evenimentul setat să vadă și termenul.
        """
        if deadline is not None:
            self._hash_deadline = deadline

        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        """Așteaptă terminarea firului, inclusiv drenarea finală."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        """Dacă firul de hashing mai rulează (inclusiv în drenare)."""
        return self._thread is not None and self._thread.is_alive()

    # -------------------------------------------------------------- prelucrare

    def process_once(self) -> int:
        """
        O trecere: reîncercările de livrare, degradările, apoi o intrare din
        coada de hashing.

        O SINGURĂ intrare hash-uită per trecere, deliberat: reîncercările și
        degradările sunt ieftine și nu trebuie să aștepte după un fișier de
        200 MB. Metoda e publică și apelabilă direct din teste, fără fir.

        Returnează numărul de evenimente predate cu succes.
        """
        emitted = self._flush_retries(fresh=True)
        emitted += self._flush_degraded("skipped_capacity")

        pending = self._take_next()

        if pending is not None:
            emitted += self._process(pending)

        return emitted

    def drain_for_shutdown(self) -> int:
        """
        Ultima acțiune a firului: golește coada, hash-uind cât încape în termen.

        Ce e mărginit de termen și ce NU
        --------------------------------
        Termenul mărginește HASHING-ul. Fiecare fișier e verificat înainte de
        pornire ȘI între blocurile de citire, deci nicio citire în curs nu-l
        poate depăși cu mai mult de un bloc.

        Termenul NU mărginește RAPORTAREA, deliberat. Ce n-a apucat să fie
        hash-uit — inclusiv o citire abandonată la jumătate — se emite cu
        hash_status='skipped_shutdown'. Un fișier nehash-uit pierde doar
        identitatea lui de conținut; un fișier neraportat dispare cu totul.
        Raportarea rămâne mărginită structural, de MAX_HASH_QUEUE_DEPTH scrieri
        în spool, nu de un ceas.

        Termenul vine de la stop(); dacă nimeni nu l-a impus (teste, sau
        FileMonitor.join fără timeout), se cade înapoi pe bugetul implicit,
        măsurat de aici.

        Reîncercările de livrare nu se mai rețin aici: treceri următoare nu mai
        există, deci fiecare payload primește exact o șansă.
        """
        if self._hash_deadline is None:
            self._hash_deadline = self._clock() + self.shutdown_budget_seconds

        deadline = self._hash_deadline
        emitted = 0

        while self._clock() < deadline:
            pending = self._take_next()

            if pending is None:
                break

            emitted += self._process(pending, final_attempt=True)

        emitted += self._flush_degraded("skipped_shutdown", final_attempt=True)
        emitted += self._flush_retries(fresh=False)

        remaining = self._drain_queue()

        if remaining:
            self.logger.warning(
                "Shutdown hash budget expired with %s file(s) unhashed; "
                "reporting them without a hash.",
                len(remaining),
            )

        for pending in remaining:
            emitted += self._emit(
                self._build(pending, _HashOutcome("skipped_shutdown")),
                self.max_emit_attempts - 1,
            )

        return emitted

    def _take_next(self) -> Optional[PendingFile]:
        with self._lock:
            return self._queue.popleft() if self._queue else None

    def _drain_queue(self) -> List[PendingFile]:
        with self._lock:
            drained = list(self._queue)
            self._queue.clear()
            return drained

    def _flush_degraded(self, status: str, final_attempt: bool = False) -> int:
        with self._lock:
            degraded = list(self._degraded)
            self._degraded.clear()

        attempts = self.max_emit_attempts - 1 if final_attempt else 0
        emitted = 0

        for pending in degraded:
            emitted += self._emit(self._build(pending, _HashOutcome(status)), attempts)

        return emitted

    def _flush_retries(self, fresh: bool) -> int:
        pending_retries = self._retry
        self._retry = []
        emitted = 0

        for payload, attempts in pending_retries:
            emitted += self._emit(
                payload, attempts if fresh else self.max_emit_attempts - 1
            )

        return emitted

    def _process(self, pending: PendingFile, final_attempt: bool = False) -> int:
        """Hash-uiește o intrare și fie o emite, fie o reintroduce."""
        outcome = self.hash_file(pending.path)

        if outcome.changed:
            if pending.reintroduction_count >= self.max_reintroductions:
                # Plafonul de reintroduceri e aceeași apărare ca plafonul de
                # așteptare al tracker-ului, în alt strat: orice buclă de
                # reîncercare fără termen limită poate rula la nesfârșit.
                # 'unstable' nu e un eșec, e o observație — un fișier rescris
                # continuu e informație de detecție reală.
                self.logger.info(
                    "File still changing after %s hash attempts; reporting it "
                    "as unstable: %s",
                    pending.reintroduction_count,
                    pending.path,
                )
                outcome = _HashOutcome(
                    "unstable",
                    file_size=outcome.file_size,
                    duration_ms=outcome.duration_ms,
                )
            elif final_attempt:
                # La oprire nu mai există cine să reia așteptarea.
                outcome = _HashOutcome(
                    "skipped_shutdown",
                    file_size=outcome.file_size,
                    duration_ms=outcome.duration_ms,
                )
            else:
                self.logger.debug(
                    "File changed while hashing; putting it back to settle: %s",
                    pending.path,
                )
                self.tracker.reintroduce(pending)
                return 0

        attempts = self.max_emit_attempts - 1 if final_attempt else 0
        return self._emit(self._build(pending, outcome), attempts)

    # ------------------------------------------------------------------ hash

    def hash_file(self, file_path: str) -> _HashOutcome:
        """
        Calculează SHA-256 cu verificare dublă. Nu ridică excepții: fiecare mod
        de eșec are un hash_status care îl numește.

        Ordinea contează. stat() ÎNAINTE de deschidere, ca plafonul de
        dimensiune să fie verificat fără să plătim citirea pe care vrem s-o
        evităm. stat() DUPĂ citire, pe același fișier, ca să știm dacă
        rezultatul descrie o stare reală.
        """
        try:
            before = os.stat(file_path)
        except FileNotFoundError:
            return _HashOutcome("vanished")
        except OSError as error:
            self.logger.debug("Could not stat %s: %s", file_path, error)
            return _HashOutcome("unreadable")

        if before.st_size > self.max_file_size_bytes:
            # file_size vine din stat(), deci rămâne cunoscut chiar dacă
            # fișierul nu a fost citit niciodată.
            return _HashOutcome("too_large", file_size=before.st_size)

        digest = hashlib.sha256()
        started = self._clock()

        try:
            with open(file_path, "rb") as handle:
                while True:
                    # Termenul se verifică ÎNTRE blocuri, nu doar înainte de
                    # fișier. Verificat doar la început, un fișier de 200 MB
                    # pornit cu o milisecundă înainte de expirare se citea
                    # integral: termenul mărginea momentul ultimei porniri, nu
                    # durata muncii. Granularitatea e blocul de 1 MiB —
                    # milisecunde, nu zeci de secunde.
                    deadline = self._hash_deadline

                    if deadline is not None and self._clock() >= deadline:
                        # WARNING, nu DEBUG: e o citire întreagă aruncată, deci
                        # un fișier care rămâne fără identitate de conținut.
                        # Nu poate deveni zgomot — bucla de drenare se oprește
                        # la termen, deci cel mult UN fișier e abandonat la
                        # jumătate; restul intră în avertismentul agregat.
                        self.logger.warning(
                            "Shutdown deadline reached mid-read; abandoning "
                            "the hash of %s.",
                            file_path,
                        )
                        return _HashOutcome(
                            "skipped_shutdown",
                            file_size=before.st_size,
                            duration_ms=int(
                                round((self._clock() - started) * 1000)
                            ),
                        )

                    chunk = handle.read(_HASH_CHUNK_SIZE)

                    if not chunk:
                        break

                    digest.update(chunk)
        except FileNotFoundError:
            return _HashOutcome("vanished")
        except OSError as error:
            self.logger.debug("Could not read %s: %s", file_path, error)
            return _HashOutcome("unreadable")

        duration_ms = int(round((self._clock() - started) * 1000))

        try:
            after = os.stat(file_path)
        except FileNotFoundError:
            return _HashOutcome("vanished", duration_ms=duration_ms)
        except OSError as error:
            self.logger.debug("Could not re-stat %s: %s", file_path, error)
            return _HashOutcome("unreadable", duration_ms=duration_ms)

        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return _HashOutcome(
                "unstable",
                file_size=after.st_size,
                duration_ms=duration_ms,
                changed=True,
            )

        return _HashOutcome(
            "ok",
            sha256=digest.hexdigest(),
            file_size=after.st_size,
            duration_ms=duration_ms,
        )

    # --------------------------------------------------------------- emitere

    def _build(self, pending: PendingFile, outcome: _HashOutcome) -> Dict[str, Any]:
        return build_file_event_payload(
            agent_id=self.agent_id,
            event_type=pending.event_type,
            file_path=pending.path,
            agent_instance_id=self.agent_instance_id,
            occurred_at=pending.occurred_at,
            settle_wait_ms=pending.settle_wait_ms,
            sha256=outcome.sha256,
            hash_status=outcome.hash_status,
            file_size=outcome.file_size,
            hash_duration_ms=outcome.duration_ms,
        )

    def _emit(self, payload: Dict[str, Any], attempts_so_far: int) -> int:
        """
        O încercare de livrare. Payload-ul se construiește o singură dată și se
        reține ca atare între reîncercări, deci client_event_id rămâne stabil —
        de el depinde deduplicarea at-least-once a spool-ului dacă o încercare
        a apucat să producă efecte parțiale înainte să eșueze.
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
            "Reported %s event for file: %s | hash_status=%s",
            payload.get("event_type"),
            payload.get("file_path"),
            payload.get("hash_status"),
        )
        return 1