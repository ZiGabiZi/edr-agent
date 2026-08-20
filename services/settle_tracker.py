import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Callable, List, Optional


# Cât timp trebuie să treacă fără nicio observație nouă înainte ca un fișier
# să fie considerat stabilizat.
DEFAULT_SETTLE_QUIET_SECONDS = 1.0

# Plafon dur: după atâta timp de la prima observație, fișierul este eliberat
# chiar dacă încă se modifică.
#
# Fără acest plafon, un fișier care se schimbă la nesfârșit (un log în care se
# scrie continuu) nu ar fi raportat niciodată — exact bug-ul pe care acest
# mecanism îl înlocuiește, rescris în altă formă. Regula generală: orice
# mecanism de suprimare fără termen limită poate suprima pentru totdeauna.
DEFAULT_MAX_SETTLE_WAIT_SECONDS = 60.0

# Plafon pentru numărul de fișiere urmărite simultan.
_SETTLE_MAX_PENDING_FILES = 10_000


def _default_wall_clock() -> str:
    """Ceasul de perete, folosit exclusiv pentru occurred_at."""
    return datetime.now(timezone.utc).isoformat()


def _normalize(file_path: str) -> str:
    return os.path.abspath(file_path)


@dataclass
class PendingFile:
    """
    Un fișier observat, aflat în așteptare sau tocmai eliberat.
    """

    path: str #calea normalizată, așa cum va fi raportată
    event_type: str #tipul înghețat la prima observație (vezi Decizia 1)
    occurred_at: str #ceasul de perete la PRIMA observație (Decizia 1)
    first_seen: float #ceasul monoton la prima observație
    last_seen: float #ceasul monoton la ultima observație
    released_at: Optional[float] = None #ceasul monoton la eliberare (None cât e în așteptare)
    observation_count: int = 1 #câte evenimente watchdog au fost absorbite
    forced_reason: Optional[str] = None #None dacă s-a stabilizat natural; altfel motivul eliberării forțate "ceiling" | "capacity" | "shutdown"


    @property
    def settle_wait_ms(self) -> Optional[int]:
        """
        Latența totală introdusă de mecanism: de la prima observație până la
        eliberare, incluzând perioada de liniște.

        Nu este „cât timp s-a scris fișierul" (acela ar fi last_seen -
        first_seen). Este costul observației, adică exact ce declară
        contracts/wire-contract.json la models.event_measurements.
        """
        if self.released_at is None:
            return None
        return int(round((self.released_at - self.first_seen) * 1000))


class SettleTracker:
    """
    Decide când un fișier a încetat să se schimbe.

    Înlocuiește EventDebouncer pentru evenimentele de fișier. Diferența nu este
    de implementare, ci de întrebare: debouncer-ul răspundea la „am mai văzut
    asta recent?", iar T0 are nevoie de răspuns la „s-a terminat de scris?".
    A doua întrebare nu se poate deduce din prezența observațiilor, ci din
    absența lor.

    Mecanismul este un planificator, nu un filtru: observe() nu decide nimic și
    revine imediat (este apelat de pe firul watchdog), iar due() colectează ce
    a devenit între timp gata de raportat (este apelat de pe firul worker).
    Tracker-ul nu apelează niciun callback și nu atinge discul — deține
    exclusiv politica de timp.

    IMPORTANT: watchdog nu garantează un eveniment pentru fiecare scriere, deci
    ce produce acest tracker este un CANDIDAT, nu un adevăr. Confirmarea reală
    (recitirea size/mtime în jurul hashing-ului, cu reintroducere în așteptare
    dacă fișierul s-a mișcat) aparține Pasului 0.3.

    Memoria este mărginită de două mecanisme independente, ca la EventDebouncer,
    dar cu o diferență esențială la depășirea capacității: acolo, evicțiunea
    unei chei producea cel mult un duplicat raportat; aici ar produce un
    eveniment PIERDUT, adică exact bug-ul reparat. De aceea la plafon nu se
    evacuează, ci se eliberează forțat.
    """

    def __init__(
        self,
        quiet_seconds: float = DEFAULT_SETTLE_QUIET_SECONDS,
        max_wait_seconds: float = DEFAULT_MAX_SETTLE_WAIT_SECONDS,
        max_pending_files: int = _SETTLE_MAX_PENDING_FILES,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], str] = _default_wall_clock,
        logger: Optional[logging.Logger] = None,
    ):
        self.quiet_seconds = quiet_seconds
        self.max_wait_seconds = max_wait_seconds
        self.max_pending_files = max_pending_files

        # Două ceasuri, deliberat. Duratele se măsoară monoton, ca să fie imune
        # la ajustări NTP sau la schimbarea orei sistemului; ceasul de perete
        # este folosit exclusiv pentru occurred_at, care trebuie să fie o dată
        # reală, comparabilă între agenți.
        self._clock = clock
        self._wall_clock = wall_clock

        self.logger = logger or logging.getLogger(__name__)

        # Ordinea este ordinea PRIMEI observații și nu se rearanjează niciodată
        # (fără move_to_end). Diferență intenționată față de EventDebouncer:
        # acolo ordinea era „ultima apariție", pentru că se evacua ce fusese
        # atins cel mai demult. Aici, la presiune, trebuie eliberate intrările
        # care au așteptat cel mai mult — deci ordinea corectă este cea de
        # intrare. Aceeași structură de date, semantică opusă.
        self._pending: "OrderedDict[str, PendingFile]" = OrderedDict()

        # Intrări deja decise, care așteaptă doar să fie colectate de due().
        # Există ca observe() să poată forța o eliberare la depășirea
        # capacității fără să emită nimic el însuși — firul watchdog nu trebuie
        # să ajungă niciodată să raporteze.
        self._ready: List[PendingFile] = []

        self._lock = Lock()

    def observe(self, file_path: str, event_type: str) -> None:
        """
        Înregistrează o observație watchdog. Revine imediat, nu decide nimic.

        Cheia este calea, nu perechea (tip, cale): un fișier creat și apoi scris
        de opt ori este o singură sosire, nu două evenimente. Tipul rămâne cel
        al observației care a deschis intrarea — vezi Decizia 1 și nota din
        contracts/wire-contract.json despre event_type ca observație locală,
        nu ca afirmație autoritară despre noutate.
        """
        normalized_path = _normalize(file_path)
        key = os.path.normcase(normalized_path)
        now = self._clock()

        with self._lock:
            existing = self._pending.get(key)

            if existing is not None:
                existing.last_seen = now
                existing.observation_count += 1
                return

            self._pending[key] = PendingFile(
                path=normalized_path,
                event_type=event_type,
                occurred_at=self._wall_clock(),
                first_seen=now,
                last_seen=now,
            )

            self._force_release_over_capacity(now)

    def due(self) -> List[PendingFile]:
        """
        Returnează și elimină fișierele gata de raportat.

        Un fișier este gata fie pentru că s-a liniștit (nicio observație nouă
        de quiet_seconds), fie pentru că a atins plafonul de așteptare.

        Returnarea elimină intrarea: o intrare nu poate fi returnată de două ori.
        """
        now = self._clock()
        released: List[PendingFile] = []

        with self._lock:
            released.extend(self._ready)
            self._ready.clear()

            expired_keys = []

            for key, pending in self._pending.items():
                settled_naturally = (now - pending.last_seen) >= self.quiet_seconds
                hit_ceiling = (now - pending.first_seen) >= self.max_wait_seconds

                if settled_naturally:
                    pending.forced_reason = None
                elif hit_ceiling:
                    pending.forced_reason = "ceiling"
                    self.logger.warning(
                        "File released at the settle ceiling while still changing: %s "
                        "(%s observations in %.1fs)",
                        pending.path,
                        pending.observation_count,
                        now - pending.first_seen,
                    )
                else:
                    continue

                pending.released_at = now
                released.append(pending)
                expired_keys.append(key)

            for key in expired_keys:
                del self._pending[key]

        return released

    def flush(self) -> List[PendingFile]:
        """
        Eliberează tot ce este în așteptare, indiferent de ceas.

        Folosit la oprirea agentului: intrările rămase în așteptare ar fi altfel
        pierdute complet. Comportamentul din Pasul 0.2 este eliberarea imediată;
        persistarea lor în spool pentru reluare după repornire este Pasul 0.2b.
        """
        now = self._clock()

        with self._lock:
            released = list(self._ready)
            self._ready.clear()

            for pending in self._pending.values():
                pending.forced_reason = "shutdown"
                pending.released_at = now
                released.append(pending)

            self._pending.clear()

        return released

    def pending_count(self) -> int:
        """Câte fișiere sunt în așteptare (fără cele deja decise)."""
        with self._lock:
            return len(self._pending)

    def _force_release_over_capacity(self, now: float) -> None:
        """
        Menține numărul de intrări în așteptare sub plafon.

        Se apelează cu lock-ul deja ținut.

        Spre deosebire de EventDebouncer._evict_over_capacity, care ARUNCA
        intrarea, aici intrarea este ELIBERATĂ. Compromisul este inversat
        deliberat: la debouncer, pierderea unei chei producea cel mult un
        duplicat raportat; aici ar produce un eveniment pierdut — exact bug-ul
        pe care mecanismul îl repară. Presiunea de memorie se convertește în
        raportare prematură, niciodată în tăcere.
        """
        while len(self._pending) > self.max_pending_files:
            _, oldest = self._pending.popitem(last=False)
            oldest.forced_reason = "capacity"
            oldest.released_at = now
            self._ready.append(oldest)

            self.logger.warning(
                "Settle tracker is over capacity (%s pending); releasing %s early.",
                self.max_pending_files,
                oldest.path,
            )