"""
Coadă persistentă de evenimente (store-and-forward) pentru agentul EDR.
=======================================================================

Problema rezolvată:
    Evenimentele de fișier erau trimise direct pe rețea de pe thread-ul
    watchdog (fire-and-forget). Orice eșec de transport — server oprit,
    rețea căzută sau agent necunoscut de server (404 după un restart cu
    store volatil) — ducea la pierderea definitivă a evenimentului:
    payload-ul exista doar în memorie și era aruncat după un warning.

Soluția:
    Detecția și livrarea sunt decuplate printr-o coadă durabilă pe disc:

        watchdog (producător) ──▶ EventSpool (SQLite) ──▶ EventDispatcher ──▶ server

    - enqueue() este o operație strict locală: reușește indiferent de
      starea rețelei sau a serverului.
    - Evenimentele sunt șterse DOAR după confirmarea trimiterii
      (semantică at-least-once).
    - Coada supraviețuiește repornirii agentului și a sistemului.
    - Dimensiunea cozii este plafonată; la depășire se elimină cele mai
      vechi evenimente (eviction FIFO), cu logare explicită.

Alegeri de implementare:
    - journal_mode=WAL: rezistență la crash și acces concurent între
      thread-ul watchdog (enqueue) și thread-ul dispatcher (peek/delete).
    - O singură conexiune partajată (check_same_thread=False) serializată
      printr-un threading.Lock propriu — simplu și suficient pentru
      debitul de evenimente al unui singur agent.
"""

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SPOOL_PATH = _BASE_DIR / "event_spool.db"
DEFAULT_MAX_SPOOLED_EVENTS = 10_000


class EventSpoolError(Exception):
    """Eroare ridicată atunci când coada persistentă de evenimente nu poate fi accesată."""
    pass


class EventSpool:
    """
    Coadă FIFO persistentă pe disc (SQLite) pentru evenimentele agentului.

    Garanții:
      - enqueue() nu atinge rețeaua, deci nu eșuează din cauza serverului.
      - Un eveniment este șters DOAR după confirmarea trimiterii (mark_sent).
      - Coada este plafonată la max_events; la depășire se elimină cele mai
        vechi intrări, iar eliminarea este logată ca warning.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS event_spool (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            payload    TEXT    NOT NULL,
            created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            attempts   INTEGER NOT NULL DEFAULT 0
        )
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_SPOOL_PATH,
        max_events: int = DEFAULT_MAX_SPOOLED_EVENTS,
        logger: Optional[logging.Logger] = None,
    ):
        self.db_path = Path(db_path)
        self.max_events = max_events
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()

        try:
            self._connection = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute(self._SCHEMA)
            self._connection.commit()
        except sqlite3.Error as error:
            raise EventSpoolError(
                f"Could not open event spool database {self.db_path}: {error}"
            ) from error

    def enqueue(self, event_payload: Dict[str, Any]) -> None:
        """
        Persistă un eveniment în coadă. Operație strict locală (disc).

        Apelabilă de pe orice thread — inclusiv thread-ul observer-ului
        watchdog, deoarece nu implică I/O de rețea.
        """
        serialized_payload = json.dumps(event_payload, ensure_ascii=False)

        with self._lock:
            try:
                self._connection.execute(
                    "INSERT INTO event_spool (payload) VALUES (?)",
                    (serialized_payload,),
                )
                self._evict_oldest_if_full_locked()
                self._connection.commit()
            except sqlite3.Error as error:
                raise EventSpoolError(f"Could not enqueue event: {error}") from error

    def _evict_oldest_if_full_locked(self) -> None:
        """
        Elimină cele mai vechi evenimente dacă s-a depășit plafonul cozii.

        Politica FIFO: în context EDR, evenimentele recente sunt mai valoroase
        pentru răspunsul activ decât cele foarte vechi, care probabil au fost
        deja depășite operațional. Trebuie apelată doar cu _lock deținut.
        """
        (count,) = self._connection.execute(
            "SELECT COUNT(*) FROM event_spool"
        ).fetchone()

        overflow = count - self.max_events
        if overflow > 0:
            self._connection.execute(
                "DELETE FROM event_spool WHERE id IN "
                "(SELECT id FROM event_spool ORDER BY id LIMIT ?)",
                (overflow,),
            )
            self.logger.warning(
                "Event spool reached capacity (max=%d): dropped %d oldest event(s).",
                self.max_events,
                overflow,
            )

    def peek_batch(self, limit: int) -> List[Tuple[int, Dict[str, Any]]]:
        """
        Returnează cele mai vechi evenimente din coadă FĂRĂ a le șterge.

        Ștergerea se face separat, prin mark_sent(), abia după confirmarea
        trimiterii — aceasta este esența semanticii at-least-once.
        Rândurile cu JSON corupt sunt eliminate și logate, ca să nu blocheze
        restul cozii.
        """
        with self._lock:
            try:
                rows = self._connection.execute(
                    "SELECT id, payload FROM event_spool ORDER BY id LIMIT ?",
                    (limit,),
                ).fetchall()
            except sqlite3.Error as error:
                raise EventSpoolError(f"Could not read event spool: {error}") from error

        batch: List[Tuple[int, Dict[str, Any]]] = []
        corrupted_row_ids: List[int] = []

        for row_id, serialized_payload in rows:
            try:
                batch.append((row_id, json.loads(serialized_payload)))
            except ValueError:
                corrupted_row_ids.append(row_id)

        for row_id in corrupted_row_ids:
            self.logger.error(
                "Dropping corrupted spooled event (id=%d): payload is not valid JSON.",
                row_id,
            )
            self.mark_sent(row_id)

        return batch

    def mark_sent(self, row_id: int) -> None:
        """Șterge definitiv un eveniment confirmat ca trimis (sau abandonat explicit)."""
        with self._lock:
            try:
                self._connection.execute(
                    "DELETE FROM event_spool WHERE id = ?", (row_id,)
                )
                self._connection.commit()
            except sqlite3.Error as error:
                raise EventSpoolError(
                    f"Could not delete spooled event {row_id}: {error}"
                ) from error

    def record_attempt(self, row_id: int) -> None:
        """Incrementează contorul de încercări eșuate al unui eveniment (observabilitate)."""
        with self._lock:
            try:
                self._connection.execute(
                    "UPDATE event_spool SET attempts = attempts + 1 WHERE id = ?",
                    (row_id,),
                )
                self._connection.commit()
            except sqlite3.Error as error:
                raise EventSpoolError(
                    f"Could not update spooled event {row_id}: {error}"
                ) from error

    def pending_count(self) -> int:
        """Numărul de evenimente aflate încă în coadă."""
        with self._lock:
            try:
                (count,) = self._connection.execute(
                    "SELECT COUNT(*) FROM event_spool"
                ).fetchone()
            except sqlite3.Error as error:
                raise EventSpoolError(f"Could not count spooled events: {error}") from error
        return count

    def close(self) -> None:
        """Închide conexiunea la baza de date a cozii."""
        with self._lock:
            self._connection.close()