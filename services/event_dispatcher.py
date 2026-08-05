"""
Dispatcher de evenimente — consumatorul cozii persistente (store-and-forward).
==============================================================================

Rulează pe un thread dedicat și golește EventSpool către serverul EDR:

    1. Citește cele mai vechi evenimente din coadă (peek, fără ștergere).
    2. Le trimite în ordine prin transport (POST /api/events).
    3. Șterge fiecare eveniment DOAR după răspuns de succes (at-least-once).

Politica de erori — cheia întregii corecturi:

    AgentNotRegisteredError (HTTP 404 — serverul a pierdut agentul):
        Stare TEMPORARĂ. Evenimentele rămân în coadă, dispatcher-ul face
        backoff, iar heartbeat-ul existent va re-înregistra agentul (fie
        prin directiva 'reregister', fie prin propriul 404). După
        re-înregistrare, coada se golește de la sine. Nimic nu se pierde.

    TransportError (rețea căzută, timeout, 5xx):
        Stare temporară. Identic: evenimentele rămân în coadă și se
        reîncearcă cu exponential backoff + jitter (reutilizăm
        HeartbeatBackoffController, deci și aici evităm thundering herd).

    FatalTransportError (401/403/422 etc. — respins definitiv):
        Evenimentul respectiv nu va fi acceptat niciodată (ex: payload
        invalid). Este eliminat individual și logat ca eroare, altfel ar
        bloca la nesfârșit restul cozii (problema clasică "poison message").
"""

import logging
import threading
from typing import Optional

from services.backoff import HeartbeatBackoffController
from services.event_spool import EventSpool, EventSpoolError
from services.stop_signal import StopSignal
from services.transport import (
    AgentNotRegisteredError,
    FatalTransportError,
    TransportError,
    send_event,
)


DEFAULT_BATCH_SIZE = 20
DEFAULT_IDLE_POLL_SECONDS = 2.0

_DISPATCH_BASE_DELAY_SECONDS = 5.0
_DISPATCH_MAX_DELAY_SECONDS = 300.0


class EventDispatcher(threading.Thread):
    """
    Thread-ul care livrează evenimentele din coada persistentă către server.

    Producătorul (watchdog) și consumatorul (acest thread) comunică exclusiv
    prin EventSpool; wake() este doar o optimizare de latență — fără ea,
    evenimentele noi ar fi preluate oricum la următorul ciclu de polling.
    """

    def __init__(
        self,
        spool: EventSpool,
        server_url: str,
        agent_id: str,
        stop_event: StopSignal,
        logger: Optional[logging.Logger] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS,
    ):
        super().__init__(name="edr-event-dispatcher", daemon=True)

        self.spool = spool
        self.server_url = server_url
        self.stop_event = stop_event
        self.logger = logger or logging.getLogger(__name__)
        self.batch_size = batch_size
        self.idle_poll_seconds = idle_poll_seconds

        self._wakeup = threading.Event()
        self._backoff = HeartbeatBackoffController(
            agent_id=agent_id,
            base_delay=_DISPATCH_BASE_DELAY_SECONDS,
            max_delay=_DISPATCH_MAX_DELAY_SECONDS,
            logger=self.logger,
        )

    def wake(self) -> None:
        """Trezește dispatcher-ul imediat (apelat de producător după enqueue)."""
        self._wakeup.set()

    def stop(self) -> None:
        """
        Deblochează orice așteptare internă pentru un shutdown responsiv.
        Oprirea efectivă este dictată de stop_event (partajat cu agentul).
        """
        self._wakeup.set()

    def run(self) -> None:
        self.logger.info(
            "Event dispatcher started. Pending events in spool: %d.",
            self._safe_pending_count(),
        )

        while not self.stop_event.is_set():
            try:
                dispatched_count = self._dispatch_batch()
            except EventSpoolError as error:
                self.logger.error("Event spool unavailable: %s", error)
                self._wait(self.idle_poll_seconds)
                continue

            if dispatched_count == 0 and not self.stop_event.is_set():
                self._wait(self.idle_poll_seconds)

        self.logger.info(
            "Event dispatcher stopped. Events left in spool for next run: %d.",
            self._safe_pending_count(),
        )

    def _dispatch_batch(self) -> int:
        """
        Trimite un lot de evenimente în ordinea cozii (FIFO).

        Returnează numărul de evenimente livrate cu succes. La prima eroare
        temporară se oprește lotul și se așteaptă conform backoff-ului —
        ordinea evenimentelor este astfel păstrată.
        """
        batch = self.spool.peek_batch(self.batch_size)
        if not batch:
            return 0

        sent_count = 0

        for row_id, event_payload in batch:
            if self.stop_event.is_set():
                break

            try:
                response = send_event(self.server_url, event_payload)
                self.spool.mark_sent(row_id)
                sent_count += 1
                self._backoff.record_success()
                self.logger.info(
                    "Dispatched spooled event id=%d (%s). Server response: %s",
                    row_id,
                    event_payload.get("event_type", "unknown"),
                    response,
                )

            except AgentNotRegisteredError as error:
                # Serverul nu (mai) cunoaște agentul — stare temporară.
                # Evenimentele rămân în coadă până la re-înregistrarea
                # făcută de bucla de heartbeat.
                self.logger.warning(
                    "Server does not recognize this agent (%s). "
                    "Events remain spooled until re-registration completes.",
                    error,
                )
                self.spool.record_attempt(row_id)
                self._sleep_backoff(self._backoff.record_failure())
                break

            except FatalTransportError as error:
                # Respins definitiv (ex: 422 payload invalid). Eliminăm DOAR
                # acest eveniment, ca să nu blocheze restul cozii.
                self.logger.error(
                    "Spooled event id=%d permanently rejected by server and dropped: %s",
                    row_id,
                    error,
                )
                self.spool.mark_sent(row_id)

            except TransportError as error:
                self.logger.warning(
                    "Could not dispatch spooled event id=%d: %s",
                    row_id,
                    error,
                )
                self.spool.record_attempt(row_id)
                self._sleep_backoff(self._backoff.record_failure())
                break

        return sent_count

    def _wait(self, timeout: float) -> None:
        """Așteaptă, dar se trezește imediat la wake() sau stop()."""
        self._wakeup.wait(timeout=timeout)
        self._wakeup.clear()

    def _sleep_backoff(self, timeout: float) -> None:
        """
        Așteaptă backoff-ul după un eșec de transport.

        Spre deosebire de _wait(), NU este întreruptă de wake(): un eveniment de
        fișier nou nu schimbă faptul că serverul e indisponibil, deci nu trebuie
        să scurteze backoff-ul — altfel s-ar anula tocmai protecția anti-retry-storm.
        Rămâne responsivă doar la oprire, prin stop_event (setat în finally înainte
        de dispatcher.stop()).
        """
        self.stop_event.wait(timeout=timeout)

    def _safe_pending_count(self) -> int:
        try:
            return self.spool.pending_count()
        except EventSpoolError:
            return -1