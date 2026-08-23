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

    AuthenticationError (HTTP 401 — identitate nerecunoscută):
        Stare temporară din punctul de vedere al cozii. Evenimentele rămân
        pe disc, se face backoff, iar alarma escaladează pe timp scurs
        (services/auth_alarm.py). Vezi mai jos de ce NU e tratat ca fatal.

    IdentityMismatchError (HTTP 403 — cheie validă, dar a altui agent):
        Nu se repară singur, deci se raportează la ERROR de la prima
        apariție. Coada rămâne totuși intactă: evenimentele sunt reale și
        aparțin acestei mașini; doar credențiala e greșită.

    FatalTransportError (422 etc. — respins definitiv):
        Evenimentul respectiv nu va fi acceptat niciodată (ex: payload
        invalid). Este eliminat individual și logat ca eroare, altfel ar
        bloca la nesfârșit restul cozii (problema clasică "poison message").

De ce eșecurile de autentificare NU golesc coada, nici măcar cu plafon:
    Criteriul de eliminare e „proprietate a mesajului". Un payload invalid e
    stricat oricând l-ai trimite; un 401 descrie relația agent–server la
    momentul cererii. Confundându-le, o cheie greșită timp de câteva minute ar
    fi șters ireversibil tot ce era în coadă, câte un eveniment pe rând.

    Consecința de securitate e și mai directă: dacă spargerea autentificării ar
    duce la ștergerea cozii, atunci pe un endpoint compromis „strică cheia" ar
    deveni o metodă de a face dovezile să dispară de la sine. Coada crește, și e
    corect să crească; plafonul general al spool-ului rămâne un pas separat, cu
    propriile lui compromisuri.
"""

import logging
import threading
from typing import Optional

from services.agent_credentials import AgentCredentials
from services.auth_alarm import (
    DEFAULT_FIRST_ALARM_SECONDS,
    DEFAULT_REPEAT_ALARM_SECONDS,
    AuthFailureAlarm,
)
from services.backoff import HeartbeatBackoffController
from services.event_spool import EventSpool, EventSpoolError
from services.stop_signal import StopSignal
from services.transport import (
    AgentNotRegisteredError,
    AuthenticationError,
    FatalTransportError,
    IdentityMismatchError,
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
        credentials: Optional[AgentCredentials] = None,
        auth_alarm_first_seconds: float = DEFAULT_FIRST_ALARM_SECONDS,
        auth_alarm_repeat_seconds: float = DEFAULT_REPEAT_ALARM_SECONDS,
    ):
        super().__init__(name="edr-event-dispatcher", daemon=True)

        self.spool = spool
        self.server_url = server_url
        self.stop_event = stop_event
        self.logger = logger or logging.getLogger(__name__)
        self.batch_size = batch_size
        self.idle_poll_seconds = idle_poll_seconds

        # Obiectul, nu valoarea: cheia poate fi reemisă în timpul rulării (o
        # reînrolare declanșată de directiva 'reregister'), iar dispatcher-ul
        # trebuie să o vadă imediat. Citită o dată la construcție, ar fi rămas
        # blocat pe cea veche și ar fi luat 401 la nesfârșit.
        self.credentials = credentials

        self._wakeup = threading.Event()
        self._backoff = HeartbeatBackoffController(
            agent_id=agent_id,
            base_delay=_DISPATCH_BASE_DELAY_SECONDS,
            max_delay=_DISPATCH_MAX_DELAY_SECONDS,
            logger=self.logger,
        )
        self._auth_alarm = AuthFailureAlarm(
            channel="Event delivery",
            first_alarm_seconds=auth_alarm_first_seconds,
            repeat_alarm_seconds=auth_alarm_repeat_seconds,
            logger=self.logger,
        )

    @property
    def agent_key(self) -> Optional[str]:
        """Cheia curentă, citită la fiecare cerere. Vezi nota din __init__."""
        return self.credentials.agent_key if self.credentials is not None else None

    def _describe_backlog(self) -> str:
        """
        Detaliul atașat alarmelor: cât s-a adunat pe disc între timp.

        Se calculează leneș, doar când o alarmă chiar se emite — o interogare a
        cozii la fiecare eșec ar fi cost pur.
        """
        pending = self._safe_pending_count()

        if pending < 0:
            return "The spool could not be queried for its depth."

        return f"{pending} event(s) are waiting in the local spool."

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
                response = send_event(
                    self.server_url, event_payload, agent_key=self.agent_key
                )
                self.spool.mark_sent(row_id)
                sent_count += 1
                self._backoff.record_success()
                self._auth_alarm.record_success()
                self.logger.info(
                    "Dispatched spooled event id=%d (%s). Server response: %s",
                    row_id,
                    event_payload.get("event_type", "unknown"),
                    response,
                )

            except AuthenticationError as error:
                # Coada rămâne intactă. Se oprește lotul: următorul eveniment ar
                # fi trimis cu aceeași credențială respinsă, deci ar produce
                # doar încă un 401 și încă o linie de log.
                self._auth_alarm.record_failure(str(error), self._describe_backlog)
                self.spool.record_attempt(row_id)
                self._sleep_backoff(self._backoff.record_failure())
                break

            except IdentityMismatchError as error:
                # ERROR de la prima apariție: spre deosebire de 401, nu există
                # scenariu în care așteptarea repară singură problema. Coada
                # rămâne totuși intactă — evenimentele aparțin acestei mașini,
                # doar cheia e a alteia.
                self._auth_alarm.record_failure(
                    str(error),
                    self._describe_backlog,
                    attempt_level=logging.ERROR,
                )
                self.spool.record_attempt(row_id)
                self._sleep_backoff(self._backoff.record_failure())
                break

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