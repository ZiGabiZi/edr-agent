"""
Autentificarea API pe agent — clasificare, coadă, alarmă, credențiale.
======================================================================

Ordinea în care lucrurile pot să tacă, și de aceea ordinea testelor:

  1. CLASIFICAREA. 401 și 403 nu au voie să fie erori fatale. Regresia asta nu
     s-ar vedea în nicio rulare fericită: agentul ar merge perfect până în ziua
     în care cheia e greșită cinci minute, iar atunci ar șterge tăcut coada.
     Testul e o assertion pe ierarhia de excepții, pentru că exact ierarhia e
     ce citește EventDispatcher.

  2. COADA. Comportamentul dispatcher-ului la fiecare clasă de eroare — ce se
     păstrează și ce se aruncă.

  3. ALARMA. Escaladarea pe timp scurs, nu pe număr de încercări.

  4. CREDENȚIALELE. Ce se scrie pe disc, ce se șterge, și când NU se șterge.

  5. BUCLELE. Că un 401 nu mai oprește heartbeat-ul — regresia inversă, la fel
     de tăcută: un endpoint care dispare din consolă fără ca nimic să se fi
     stricat pe el.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from requests import Response

import agent
from services import transport
from services.agent_credentials import AgentCredentials, AgentCredentialsError
from services.backoff import HeartbeatBackoffController
from services.auth_alarm import (
    DEFAULT_FIRST_ALARM_SECONDS,
    DEFAULT_REPEAT_ALARM_SECONDS,
    AuthFailureAlarm,
)
from services.event_dispatcher import EventDispatcher
from services.transport import (
    AgentNotRegisteredError,
    AuthenticationError,
    FatalTransportError,
    IdentityConflictError,
    IdentityMismatchError,
    TransportError,
    handle_response,
)
from tests.test_agent_startup import FakeStopEvent


def _response(status_code: int, payload: Optional[Dict[str, Any]] = None) -> Response:
    response = Response()
    response.status_code = status_code
    response._content = json.dumps(payload or {"detail": "x"}).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    response.url = "http://server/api/events"
    return response


class FakeSpool:
    """Coadă observabilă: ce s-a marcat livrat și ce s-a marcat doar încercat."""

    def __init__(self, payloads: List[Dict[str, Any]]) -> None:
        self._rows: List[Tuple[int, Dict[str, Any]]] = list(
            enumerate(payloads, start=1)
        )
        self.marked_sent: List[int] = []
        self.recorded_attempts: List[int] = []

    def peek_batch(self, limit: int) -> List[Tuple[int, Dict[str, Any]]]:
        return self._rows[:limit]

    def mark_sent(self, row_id: int) -> None:
        self.marked_sent.append(row_id)

    def record_attempt(self, row_id: int) -> None:
        self.recorded_attempts.append(row_id)

    def pending_count(self) -> int:
        return len(self._rows) - len(self.marked_sent)


class FakeClock:
    """Ceas monoton controlat de test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_dispatcher(spool: FakeSpool, stop_event: Any) -> EventDispatcher:
    logger = logging.getLogger("test-dispatcher")
    logger.addHandler(logging.NullHandler())

    return EventDispatcher(
        spool=spool,  # type: ignore[arg-type]
        server_url="http://server",
        agent_id="endpoint-01",
        stop_event=stop_event,
        logger=logger,
    )


# ---------------------------------------------------------------------------
# 1. Clasificarea
# ---------------------------------------------------------------------------

class ErrorClassificationTests(unittest.TestCase):
    def test_401_is_not_a_fatal_error(self) -> None:
        """
        Regresia principală, exprimată pe ierarhie.

        EventDispatcher decide ștergerea unui eveniment prin
        `except FatalTransportError`. Dacă AuthenticationError ar descinde de
        acolo — direct sau prin refactorizare ulterioară — o cheie greșită ar
        redeveni o metodă de a goli coada, iar niciun test de flux nu ar
        semnala-o până în ziua în care se întâmplă.
        """
        self.assertFalse(issubclass(AuthenticationError, FatalTransportError))
        self.assertTrue(issubclass(AuthenticationError, TransportError))

    def test_403_is_not_a_fatal_error(self) -> None:
        self.assertFalse(issubclass(IdentityMismatchError, FatalTransportError))
        self.assertTrue(issubclass(IdentityMismatchError, TransportError))

    def test_401_maps_to_authentication_error(self) -> None:
        with self.assertRaises(AuthenticationError):
            handle_response(_response(401))

    def test_403_maps_to_identity_mismatch(self) -> None:
        with self.assertRaises(IdentityMismatchError):
            handle_response(_response(403))

    def test_409_is_not_a_fatal_error(self) -> None:
        """
        Un conflict de nume nu are voie să oprească agentul.

        Clasificat fatal, 409 făcea register_agent_with_retry să întoarcă False,
        iar run_agent sărea peste bucla de heartbeat direct în finally — oprind
        file monitor-ul, dispatcher-ul și spool-ul. O mașină perfect sănătoasă
        dispărea complet din consolă din cauza unei coliziuni de nume, iar
        tăcerea totală nu se distinge de un endpoint oprit sau compromis.
        """
        self.assertFalse(issubclass(IdentityConflictError, FatalTransportError))
        self.assertTrue(issubclass(IdentityConflictError, TransportError))

    def test_409_maps_to_identity_conflict(self) -> None:
        with self.assertRaises(IdentityConflictError):
            handle_response(_response(409))

    def test_404_still_maps_to_agent_not_registered(self) -> None:
        with self.assertRaises(AgentNotRegisteredError):
            handle_response(_response(404))

    def test_422_is_still_fatal(self) -> None:
        """
        Contra-testul: reclasificarea nu are voie să înmoaie și cazul în care
        eliminarea din coadă e corectă. Un payload invalid rămâne poison message.
        """
        with self.assertRaises(FatalTransportError):
            handle_response(_response(422))

    def test_5xx_stays_temporary(self) -> None:
        with self.assertRaises(TransportError) as raised:
            handle_response(_response(503))

        self.assertNotIsInstance(raised.exception, FatalTransportError)


class AuthHeaderTests(unittest.TestCase):
    def test_headers_carry_the_names_the_server_expects(self) -> None:
        headers = transport.build_auth_headers("k", "s")

        self.assertEqual(headers["X-Agent-Key"], "k")
        self.assertEqual(headers["X-Enrollment-Secret"], "s")

    def test_absent_credentials_produce_no_headers(self) -> None:
        """
        Nu antete goale: un X-Agent-Key: "" ar fi prezentat serverului drept
        cheie, iar 401-ul rezultat ar arăta identic cu o cheie greșită.
        """
        self.assertEqual(transport.build_auth_headers(None, None), {})
        self.assertEqual(transport.build_auth_headers("", ""), {})

    def test_send_event_attaches_the_agent_key(self) -> None:
        captured: Dict[str, Any] = {}

        def fake_post_request(server_url, endpoint, payload, headers=None, **kwargs):
            captured["headers"] = headers
            return {"ok": True}

        with patch.object(transport, "post_request", fake_post_request):
            transport.send_event("http://server", {"a": 1}, agent_key="the-key")

        self.assertEqual(captured["headers"], {"X-Agent-Key": "the-key"})

    def test_send_heartbeat_attaches_the_agent_key(self) -> None:
        captured: Dict[str, Any] = {}

        def fake_post_request(server_url, endpoint, payload, headers=None, **kwargs):
            captured["headers"] = headers
            return {"ok": True}

        with patch.object(transport, "post_request", fake_post_request):
            transport.send_heartbeat(
                "http://server", "endpoint-01", {"sequence": 1}, agent_key="the-key"
            )

        self.assertEqual(captured["headers"], {"X-Agent-Key": "the-key"})


# ---------------------------------------------------------------------------
# 2. Coada
# ---------------------------------------------------------------------------

class DispatcherQueueTests(unittest.TestCase):
    def test_authentication_failure_keeps_the_event_queued(self) -> None:
        spool = FakeSpool([{"event_type": "file_created"}])
        dispatcher = _make_dispatcher(spool, FakeStopEvent(trip_after_waits=1))

        def refuse(*args, **kwargs):
            raise AuthenticationError("HTTP 401")

        with patch("services.event_dispatcher.send_event", refuse):
            dispatched = dispatcher._dispatch_batch()

        self.assertEqual(dispatched, 0)
        self.assertEqual(spool.marked_sent, [], "evenimentul NU are voie să dispară")
        self.assertEqual(spool.recorded_attempts, [1])

    def test_identity_mismatch_keeps_the_event_queued(self) -> None:
        spool = FakeSpool([{"event_type": "file_created"}])
        dispatcher = _make_dispatcher(spool, FakeStopEvent(trip_after_waits=1))

        def refuse(*args, **kwargs):
            raise IdentityMismatchError("HTTP 403")

        with patch("services.event_dispatcher.send_event", refuse):
            dispatcher._dispatch_batch()

        self.assertEqual(spool.marked_sent, [])
        self.assertEqual(spool.recorded_attempts, [1])

    def test_a_rejected_payload_is_still_dropped(self) -> None:
        """Comportamentul care trebuia păstrat: poison message rămâne poison."""
        spool = FakeSpool([{"event_type": "file_created"}])
        dispatcher = _make_dispatcher(spool, FakeStopEvent(trip_after_waits=1))

        def refuse(*args, **kwargs):
            raise FatalTransportError("HTTP 422")

        with patch("services.event_dispatcher.send_event", refuse):
            dispatcher._dispatch_batch()

        self.assertEqual(spool.marked_sent, [1])

    def test_an_auth_failure_stops_the_batch_instead_of_burning_through_it(self) -> None:
        """
        Fără break, restul lotului ar fi trimis cu aceeași credențială respinsă:
        încă N cereri inutile și încă N linii de log pentru o singură cauză.
        """
        spool = FakeSpool([{"n": 1}, {"n": 2}, {"n": 3}])
        dispatcher = _make_dispatcher(spool, FakeStopEvent(trip_after_waits=1))

        attempts = []

        def refuse(server_url, payload, agent_key=None):
            attempts.append(payload)
            raise AuthenticationError("HTTP 401")

        with patch("services.event_dispatcher.send_event", refuse):
            dispatcher._dispatch_batch()

        self.assertEqual(len(attempts), 1)

    def test_the_dispatcher_picks_up_a_credential_fixed_on_disk(self) -> None:
        """
        Recuperare fără repornire, de la un capăt la altul.

        Prima încercare eșuează cu 401; între timp operatorul scrie cheia pe
        disc; a doua încercare o folosește și reușește. Fără recitirea din
        ramura de eroare, dispatcher-ul ar fi reluat la nesfârșit cu aceeași
        credențială respinsă, iar coada s-ar fi golit doar la repornirea
        procesului.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        key_path = Path(directory.name) / "agent_key"

        credentials = AgentCredentials(
            agent_key_path=key_path,
            enrollment_secret_path=Path(directory.name) / "enrollment_secret",
        )
        credentials.load()

        spool = FakeSpool([{"n": 1}])
        # Fără trip_after_waits: testul are nevoie de DOUĂ loturi consecutive,
        # iar un semnal de oprire declanșat de așteptarea de backoff a primului
        # ar face ca al doilea să iasă înainte de prima încercare.
        dispatcher = _make_dispatcher(spool, FakeStopEvent())
        dispatcher.credentials = credentials

        used: List[Optional[str]] = []

        def refuse_then_accept(server_url, payload, agent_key=None):
            used.append(agent_key)
            if agent_key is None:
                # Operatorul pune fișierul exact între cele două încercări.
                key_path.write_text("cheia-corecta\n", encoding="utf-8")
                raise AuthenticationError("HTTP 401")
            return {"ok": True}

        with patch("services.event_dispatcher.send_event", refuse_then_accept):
            dispatcher._dispatch_batch()
            dispatched = dispatcher._dispatch_batch()

        self.assertEqual(used, [None, "cheia-corecta"])
        self.assertEqual(dispatched, 1)
        self.assertEqual(spool.marked_sent, [1])

    def test_the_dispatcher_sends_the_current_key_not_a_snapshot(self) -> None:
        """
        Cheia se citește la fiecare cerere. Dacă dispatcher-ul ar fi reținut
        valoarea de la construcție, o cheie reemisă în timpul rulării (o
        reînrolare declanșată de 'reregister') l-ar fi lăsat blocat pe cea
        veche, cu 401 la nesfârșit și cu o coadă care crește degeaba.
        """
        credentials = AgentCredentials(
            agent_key_path=Path(tempfile.gettempdir()) / "unused-key",
            enrollment_secret_path=Path(tempfile.gettempdir()) / "unused-secret",
        )
        spool = FakeSpool([{"n": 1}])
        dispatcher = _make_dispatcher(spool, FakeStopEvent(trip_after_waits=1))
        dispatcher.credentials = credentials

        self.assertIsNone(dispatcher.agent_key)

        credentials._agent_key = "issued-later"
        self.assertEqual(dispatcher.agent_key, "issued-later")

        used: List[Optional[str]] = []

        def capture(server_url, payload, agent_key=None):
            used.append(agent_key)
            return {"ok": True}

        with patch("services.event_dispatcher.send_event", capture):
            dispatcher._dispatch_batch()

        self.assertEqual(used, ["issued-later"])


# ---------------------------------------------------------------------------
# 3. Alarma
# ---------------------------------------------------------------------------

class AuthAlarmSettingsTests(unittest.TestCase):
    """
    Traseul reglajelor: din config.json până în alarma fiecărui canal.

    Clasa de bug pe care o închid e cea mai tăcută dintre toate: o cheie poate
    exista în config, poate fi validată la încărcare, și totuși nimeni să n-o
    citească. Agentul pornește, pare configurat, și rulează pe implicit.
    Aceeași gardă există deja pentru reglajele fluxului de fișiere.

    Al doilea motiv e tipul. Pragurile ajung în constructorul lui
    AuthFailureAlarm, care are și un parametru `clock` de tip Callable. Cât timp
    erau despachetate dintr-un dicționar cu **, orice cheie scrisă greșit ar fi
    devenit TypeError abia la rulare, în bucla de înregistrare a unui agent din
    producție.
    """

    def test_settings_come_from_config(self) -> None:
        settings = agent._auth_alarm_settings(
            {"auth_alarm_first_seconds": 30, "auth_alarm_repeat_seconds": 90}
        )

        self.assertEqual(settings.first_alarm_seconds, 30.0)
        self.assertEqual(settings.repeat_alarm_seconds, 90.0)
        self.assertIsInstance(settings.first_alarm_seconds, float)

    def test_absent_keys_fall_back_to_the_module_defaults(self) -> None:
        """
        O cheie lipsă înseamnă „folosește implicitul modulului", nu None și nici
        o a doua copie a valorii ținută în alt fișier.
        """
        settings = agent._auth_alarm_settings({})

        self.assertEqual(settings.first_alarm_seconds, DEFAULT_FIRST_ALARM_SECONDS)
        self.assertEqual(settings.repeat_alarm_seconds, DEFAULT_REPEAT_ALARM_SECONDS)

    def test_both_alarm_channels_receive_the_configured_thresholds(self) -> None:
        config = {"auth_alarm_first_seconds": 12, "auth_alarm_repeat_seconds": 34}

        for channel in ("Registration", "Heartbeat"):
            with self.subTest(channel=channel):
                alarm = agent._build_auth_alarm(channel, config)

                self.assertEqual(alarm.channel, channel)
                self.assertEqual(alarm.first_alarm_seconds, 12.0)
                self.assertEqual(alarm.repeat_alarm_seconds, 34.0)


class AuthAlarmTests(unittest.TestCase):
    def _alarm(self, clock: FakeClock) -> AuthFailureAlarm:
        logger = logging.getLogger("test-alarm")
        logger.addHandler(logging.NullHandler())

        return AuthFailureAlarm(
            channel="Test",
            first_alarm_seconds=300.0,
            repeat_alarm_seconds=1800.0,
            logger=logger,
            clock=clock,
        )

    def test_no_critical_before_the_threshold(self) -> None:
        clock = FakeClock()
        alarm = self._alarm(clock)

        with self.assertLogs("test-alarm", level="WARNING") as captured:
            alarm.record_failure("401")
            clock.advance(299.0)
            alarm.record_failure("401")

        self.assertEqual(
            [record for record in captured.records if record.levelno >= logging.CRITICAL],
            [],
        )

    def test_critical_once_the_threshold_is_crossed(self) -> None:
        clock = FakeClock()
        alarm = self._alarm(clock)

        alarm.record_failure("401")
        clock.advance(301.0)

        with self.assertLogs("test-alarm", level="CRITICAL") as captured:
            alarm.record_failure("401")

        self.assertEqual(len(captured.records), 1)

    def test_the_alarm_repeats_but_not_on_every_attempt(self) -> None:
        """
        Pragul de repetare există ca alarma să rămână citibilă: la o cădere de
        ore, un CRITICAL per încercare ar îneca logul exact când cineva îl
        citește.
        """
        clock = FakeClock()
        alarm = self._alarm(clock)

        alarm.record_failure("401")
        clock.advance(301.0)
        alarm.record_failure("401")   # alarma #1

        criticals = 0
        for _ in range(10):
            clock.advance(60.0)
            with self.assertLogs("test-alarm", level="WARNING") as captured:
                alarm.record_failure("401")
            criticals += sum(
                1 for record in captured.records if record.levelno >= logging.CRITICAL
            )

        self.assertEqual(criticals, 0, "sub intervalul de repetare, tăcere")

        clock.advance(1800.0)
        with self.assertLogs("test-alarm", level="CRITICAL") as captured:
            alarm.record_failure("401")
        self.assertEqual(len(captured.records), 1)

    def test_the_backlog_detail_is_only_computed_when_an_alarm_fires(self) -> None:
        clock = FakeClock()
        alarm = self._alarm(clock)
        calls = []

        def detail() -> str:
            calls.append(1)
            return "42 event(s) waiting."

        alarm.record_failure("401", detail)
        self.assertEqual(calls, [], "sub prag nu se interoghează coada")

        clock.advance(301.0)
        alarm.record_failure("401", detail)
        self.assertEqual(len(calls), 1)

    def test_recovery_after_an_alarm_is_as_loud_as_the_alarm(self) -> None:
        """
        O alarmă CRITICAL fără o închidere la fel de vizibilă lasă operatorul să
        caute o defecțiune care nu mai există.
        """
        clock = FakeClock()
        alarm = self._alarm(clock)

        alarm.record_failure("401")
        clock.advance(301.0)
        alarm.record_failure("401")

        with self.assertLogs("test-alarm", level="CRITICAL") as captured:
            alarm.record_success()

        self.assertEqual(len(captured.records), 1)
        self.assertFalse(alarm.is_failing)

    def test_success_without_a_prior_failure_is_silent(self) -> None:
        clock = FakeClock()
        alarm = self._alarm(clock)

        with self.assertNoLogs("test-alarm", level="DEBUG"):
            alarm.record_success()


# ---------------------------------------------------------------------------
# 4. Credențialele
# ---------------------------------------------------------------------------

class AgentCredentialsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.base = Path(self._directory.name)
        self.key_path = self.base / "agent_key"
        self.secret_path = self.base / "enrollment_secret"
        self.addCleanup(self._directory.cleanup)

    def _credentials(self) -> AgentCredentials:
        logger = logging.getLogger("test-credentials")
        logger.addHandler(logging.NullHandler())
        return AgentCredentials(self.key_path, self.secret_path, logger=logger)

    def test_load_reads_both_files_and_tolerates_absence(self) -> None:
        self.secret_path.write_text("shared-secret\n", encoding="utf-8")

        credentials = self._credentials()
        credentials.load()

        self.assertIsNone(credentials.agent_key)
        self.assertEqual(credentials.enrollment_secret, "shared-secret")

    def test_a_secret_saved_with_a_bom_is_read_cleanly(self) -> None:
        """
        Regresie dintr-o rulare reală, nu caz teoretic.

        Fișierul provizionat cu Set-Content -Encoding utf8 din PowerShell 5.1
        începe cu EF BB BF. Citit ca utf-8 curat, BOM-ul rămâne lipit de secret
        — .strip() nu îl elimină, nefiind spațiu alb — iar valoarea ajunge într-un
        antet HTTP, unde http.client o codează latin-1 și crapă cu
        UnicodeEncodeError din adâncul lui urllib3. Excepția nu e nici
        TransportError, nici ConfigError, deci trece prin toate ramurile de
        tratare și oprește agentul cu un traceback despre codecuri, în care
        fișierul cu pricina nu apare deloc.
        """
        self.secret_path.write_bytes("﻿shared-secret\n".encode("utf-8"))

        credentials = self._credentials()
        credentials.load()

        self.assertEqual(credentials.enrollment_secret, "shared-secret")

    def test_a_key_saved_with_a_bom_is_read_cleanly(self) -> None:
        self.key_path.write_bytes("﻿issued-key\n".encode("utf-8"))

        credentials = self._credentials()
        credentials.load()

        self.assertEqual(credentials.agent_key, "issued-key")

    def test_a_credential_that_cannot_be_a_header_is_refused_not_crashed(self) -> None:
        """
        Ultima gardă înaintea rețelei.

        Un fișier salvat într-o codificare exotică poate conține caractere pe
        care antetele HTTP nu le pot purta. Agentul trebuie să le trateze ca
        lipsă de credențială — reîncearcă, escaladează, nu pierde evenimente —
        nu să moară cu un traceback din urllib3.
        """
        self.secret_path.write_text("secret-cu-☃", encoding="utf-8")

        credentials = self._credentials()
        with self.assertLogs("test-credentials", level="ERROR"):
            credentials.load()

        self.assertIsNone(credentials.enrollment_secret)

    def test_an_emptied_secret_file_reads_as_absent(self) -> None:
        """Fișier gol = secret consumat. Nu trebuie să reintre în circulație."""
        self.secret_path.write_text("", encoding="utf-8")

        credentials = self._credentials()
        credentials.load()

        self.assertIsNone(credentials.enrollment_secret)

    def test_adopting_a_key_persists_it_for_the_next_run(self) -> None:
        credentials = self._credentials()
        credentials.adopt_agent_key("issued-key")

        self.assertEqual(credentials.agent_key, "issued-key")
        self.assertEqual(self.key_path.read_text(encoding="utf-8").strip(), "issued-key")

        reloaded = self._credentials()
        reloaded.load()
        self.assertEqual(reloaded.agent_key, "issued-key")

    def test_consuming_the_enrollment_secret_removes_it(self) -> None:
        self.secret_path.write_text("shared-secret\n", encoding="utf-8")
        credentials = self._credentials()
        credentials.load()

        credentials.consume_enrollment_secret()

        self.assertIsNone(credentials.enrollment_secret)
        self.assertFalse(self.secret_path.exists())

    def test_a_reregistration_response_does_not_consume_the_secret(self) -> None:
        """
        Serverul nu emite cheie când acceptă o re-înregistrare pe baza celei
        existente. Dacă agentul ar consuma secretul oricum, ar arunca singura
        cale de recuperare pentru o cerere care n-a schimbat nimic.
        """
        self.secret_path.write_text("shared-secret\n", encoding="utf-8")
        credentials = self._credentials()
        credentials.load()

        agent._adopt_issued_credentials({"message": "ok"}, credentials)

        self.assertEqual(credentials.enrollment_secret, "shared-secret")
        self.assertTrue(self.secret_path.exists())
        self.assertIsNone(credentials.agent_key)

    def test_a_secret_provisioned_while_running_is_picked_up(self) -> None:
        """
        Scenariul de pe un endpoint proaspăt instalat, reprodus.

        Agentul pornește fără nicio credențială, ia 401, iar operatorul pune
        fișierul cu secretul în timp ce procesul rulează. Fără recitire, bucla
        ar reîncerca la nesfârșit cu aceleași credențiale inexistente, iar
        singura ieșire ar fi repornirea — adică exact opusul motivului pentru
        care nu abandonăm la 401.
        """
        credentials = self._credentials()
        credentials.load()
        self.assertIsNone(credentials.enrollment_secret)

        self.secret_path.write_text("provizionat-tarziu\n", encoding="utf-8")

        self.assertTrue(credentials.refresh_from_disk())
        self.assertEqual(credentials.enrollment_secret, "provizionat-tarziu")

    def test_a_replaced_key_is_picked_up(self) -> None:
        self.key_path.write_text("cheie-veche\n", encoding="utf-8")
        credentials = self._credentials()
        credentials.load()

        self.key_path.write_text("cheie-noua\n", encoding="utf-8")

        self.assertTrue(credentials.refresh_from_disk())
        self.assertEqual(credentials.agent_key, "cheie-noua")

    def test_refresh_reports_no_change_when_disk_matches_memory(self) -> None:
        """Fără asta, fiecare reîncercare ar produce o linie de log inutilă."""
        self.key_path.write_text("aceeasi-cheie\n", encoding="utf-8")
        credentials = self._credentials()
        credentials.load()

        self.assertFalse(credentials.refresh_from_disk())

    def test_refresh_never_wipes_a_key_held_only_in_memory(self) -> None:
        """
        Garda care face diferența dintre adoptă și sincronizează.

        adopt_agent_key() păstrează cheia în memorie chiar dacă scrierea pe disc
        a eșuat — un disc plin nu are voie să producă un agent mut. O recitire
        care ar sincroniza orbește ar arunca exact acea copie funcțională, la
        primul 401, transformând o eroare recuperabilă într-una definitivă.
        """
        credentials = self._credentials()
        credentials._agent_key = "cheie-doar-in-memorie"

        self.assertFalse(self.key_path.exists())
        self.assertFalse(credentials.refresh_from_disk())
        self.assertEqual(credentials.agent_key, "cheie-doar-in-memorie")

    def test_an_enrollment_response_adopts_and_consumes(self) -> None:
        self.secret_path.write_text("shared-secret\n", encoding="utf-8")
        credentials = self._credentials()
        credentials.load()

        agent._adopt_issued_credentials({"agent_key": "issued-key"}, credentials)

        self.assertEqual(credentials.agent_key, "issued-key")
        self.assertIsNone(credentials.enrollment_secret)
        self.assertFalse(self.secret_path.exists())

    def test_adoption_reports_when_the_key_never_reached_the_disk(self) -> None:
        """
        Semnalul de care depinde decizia de mai jos.

        Cât timp adopt_agent_key() returna None și pe calea reușită, și pe cea
        eșuată, apelantul n-avea cum să afle că cheia trăiește doar în memorie.
        """
        credentials = self._credentials()

        with patch.object(
            credentials,
            "_write_secret_file",
            side_effect=AgentCredentialsError("disc plin"),
        ):
            with self.assertLogs("test-credentials", level="ERROR"):
                persisted = credentials.adopt_agent_key("issued-key")

        self.assertFalse(persisted)
        self.assertEqual(credentials.agent_key, "issued-key")
        self.assertFalse(self.key_path.exists())

    def test_a_key_that_could_not_be_written_keeps_the_secret(self) -> None:
        """
        Criteriul din #18: agentul trebuie să rămână recuperabil.

        Dacă scrierea cheii eșuează, cheia dispare la oprirea procesului. Un
        secret consumat oricum ar lăsa următoarea pornire fără nicio
        credențială — agentul refuză orice trimitere, umple spool-ul local și
        nu mai iese din starea asta decât dacă vine cineva să pună un fișier cu
        mâna. Secretul păstrat înseamnă că repornirea se reînrolează singură,
        exact ce promitea logul.
        """
        self.secret_path.write_text("shared-secret\n", encoding="utf-8")
        credentials = self._credentials()
        credentials.load()

        with patch.object(
            credentials,
            "_write_secret_file",
            side_effect=AgentCredentialsError("disc plin"),
        ):
            with self.assertLogs(agent.logger, level="ERROR"):
                agent._adopt_issued_credentials(
                    {"agent_key": "issued-key"}, credentials
                )

        self.assertEqual(credentials.agent_key, "issued-key")
        self.assertEqual(credentials.enrollment_secret, "shared-secret")
        self.assertTrue(self.secret_path.exists())

        reloaded = self._credentials()
        reloaded.load()
        self.assertIsNone(reloaded.agent_key)
        self.assertEqual(reloaded.enrollment_secret, "shared-secret")


# ---------------------------------------------------------------------------
# 5. Buclele
# ---------------------------------------------------------------------------

def _heartbeat_config() -> Dict[str, Any]:
    return {
        "agent_id": "endpoint-01",
        "agent_version": "0.1.0",
        "agent_instance_id": "inst-A",
        "auth_alarm_first_seconds": 300.0,
        "auth_alarm_repeat_seconds": 1800.0,
    }


class BackoffChannelNamingTests(unittest.TestCase):
    """
    Fiecare buclă își spune pe nume în jurnal.

    HeartbeatBackoffController e folosit de trei bucle independente, dar loga
    „Heartbeat failed" din toate. În testarea manuală, linii de eșec de heartbeat
    au apărut înainte ca bucla de heartbeat să fi pornit măcar, chiar lângă
    eșecul real de pe alt canal. Într-o depanare, e genul de linie care trimite
    în locul greșit — și a făcut-o de două ori într-o singură sesiune.
    """

    def _controller(self, **kwargs: Any) -> HeartbeatBackoffController:
        logger = logging.getLogger("test-backoff")
        logger.addHandler(logging.NullHandler())
        return HeartbeatBackoffController(
            agent_id="endpoint-01", base_delay=1.0, logger=logger, **kwargs
        )

    def test_the_channel_name_appears_in_the_failure_line(self) -> None:
        controller = self._controller(channel="Event delivery")

        with self.assertLogs("test-backoff", level="WARNING") as captured:
            controller.record_failure()

        self.assertIn("Event delivery failed", captured.output[0])
        self.assertNotIn("Heartbeat", captured.output[0])

    def test_the_channel_name_appears_in_the_recovery_line(self) -> None:
        controller = self._controller(channel="Registration")
        controller.record_failure()

        with self.assertLogs("test-backoff", level="INFO") as captured:
            controller.record_success()

        self.assertIn("Registration recovered", captured.output[0])

    def test_heartbeat_stays_the_default_so_its_lines_are_unchanged(self) -> None:
        """
        Implicitul păstrează textul de dinaintea parametrului. Singurele linii
        care se schimbă sunt exact cele care minteau.
        """
        controller = self._controller()

        with self.assertLogs("test-backoff", level="WARNING") as captured:
            controller.record_failure()

        self.assertIn("Heartbeat failed (consecutive: 1", captured.output[0])

    def _recorded_channels(self):
        """Interceptează numele canalului cu care fiecare buclă își face controllerul."""
        channels: List[Optional[str]] = []
        real = HeartbeatBackoffController

        def recording(*args: Any, **kwargs: Any) -> HeartbeatBackoffController:
            channels.append(kwargs.get("channel"))
            return real(*args, **kwargs)

        return channels, recording

    def test_the_registration_loop_names_its_channel(self) -> None:
        channels, recording = self._recorded_channels()

        with patch.object(agent, "HeartbeatBackoffController", recording), \
             patch.object(agent, "check_server_health", lambda url: {"status": "ok"}), \
             patch.object(agent, "register_agent", lambda *a, **k: {}):
            agent.register_agent_with_retry(
                {"agent_id": "endpoint-01", "agent_version": "0.1.0"},
                "http://server",
                {},
                FakeStopEvent(),
            )

        self.assertEqual(channels, ["Registration"])

    def test_the_heartbeat_loop_names_its_channel(self) -> None:
        channels, recording = self._recorded_channels()

        def stop_immediately(*args: Any, **kwargs: Any):
            raise TransportError("stop here")

        with patch.object(agent, "HeartbeatBackoffController", recording), \
             patch.object(agent, "send_heartbeat", stop_immediately):
            agent.heartbeat_loop(
                config=_heartbeat_config(),
                server_url="http://server",
                system_info={},
                heartbeat_interval_seconds=10,
                stop_event=FakeStopEvent(trip_after_waits=1),
            )

        self.assertEqual(channels, ["Heartbeat"])

    def test_the_dispatcher_names_its_channel(self) -> None:
        """
        Garda de traseu: un canal nou adăugat fără nume ar redeveni „Heartbeat"
        tăcut, iar jurnalul ar minți din nou fără ca nimic să pice.
        """
        dispatcher = _make_dispatcher(FakeSpool([]), FakeStopEvent())

        self.assertEqual(dispatcher._backoff.channel, "Event delivery")


class RegistrationConflictTests(unittest.TestCase):
    """Un conflict de identitate nu are voie să oprească procesul agent."""

    def _config(self) -> Dict[str, Any]:
        return {
            "agent_id": "endpoint-01",
            "agent_version": "0.1.0",
            "auth_alarm_first_seconds": 300.0,
            "auth_alarm_repeat_seconds": 1800.0,
        }

    def test_a_name_conflict_keeps_the_agent_retrying(self) -> None:
        attempts = []

        def conflict(server_url, payload, agent_key=None, enrollment_secret=None, **kwargs):
            attempts.append(payload["agent_id"])
            raise IdentityConflictError("HTTP 409")

        stop_event = FakeStopEvent(trip_after_waits=3)

        with patch.object(agent, "check_server_health", lambda url: {"status": "ok"}), \
             patch.object(agent, "register_agent", conflict):
            registered = agent.register_agent_with_retry(
                self._config(), "http://server", {}, stop_event
            )

        self.assertEqual(len(attempts), 3, "bucla trebuie să reîncerce, nu să abandoneze")
        self.assertFalse(registered, "ieșirea e oprirea cerută, nu abandonul la 409")

    def test_a_rejected_registration_payload_still_aborts(self) -> None:
        """
        Contra-testul: 422 la înregistrare rămâne motiv de abandon. Acolo cauza e
        un payload pe care agentul îl construiește greșit — un bug al nostru, nu
        o stare a parcului — și merită oprire zgomotoasă, nu retry la nesfârșit.
        """
        attempts = []

        def rejected(server_url, payload, agent_key=None, enrollment_secret=None, **kwargs):
            attempts.append(payload["agent_id"])
            raise FatalTransportError("HTTP 422")

        with patch.object(agent, "check_server_health", lambda url: {"status": "ok"}), \
             patch.object(agent, "register_agent", rejected):
            registered = agent.register_agent_with_retry(
                self._config(), "http://server", {}, FakeStopEvent(trip_after_waits=5)
            )

        self.assertEqual(len(attempts), 1)
        self.assertFalse(registered)


class HeartbeatLoopAuthTests(unittest.TestCase):
    def test_a_401_does_not_stop_the_heartbeat_loop(self) -> None:
        """
        Regresia inversă celei din clasificare, la fel de tăcută.

        Înainte, 401 era FatalTransportError, iar ramura respectivă face return:
        agentul nu mai trimitea niciun heartbeat până la repornirea procesului,
        iar în consolă endpoint-ul apărea mort. O rotație de cheie prinsă la
        mijloc ar fi scos mașina din monitorizare definitiv, fără ca nimic pe ea
        să fie stricat.
        """
        attempts = []

        def refuse(server_url, agent_id, heartbeat_payload, agent_key=None):
            attempts.append(heartbeat_payload["sequence"])
            raise AuthenticationError("HTTP 401")

        stop_event = FakeStopEvent(trip_after_waits=4)

        with patch.object(agent, "send_heartbeat", refuse):
            agent.heartbeat_loop(
                config=_heartbeat_config(),
                server_url="http://server",
                system_info={},
                heartbeat_interval_seconds=10,
                stop_event=stop_event,
            )

        self.assertEqual(
            attempts, [1, 2, 3, 4], "bucla trebuie să reîncerce, nu să se oprească"
        )

    def test_a_403_does_not_stop_the_heartbeat_loop_either(self) -> None:
        attempts = []

        def refuse(server_url, agent_id, heartbeat_payload, agent_key=None):
            attempts.append(heartbeat_payload["sequence"])
            raise IdentityMismatchError("HTTP 403")

        stop_event = FakeStopEvent(trip_after_waits=3)

        with patch.object(agent, "send_heartbeat", refuse):
            agent.heartbeat_loop(
                config=_heartbeat_config(),
                server_url="http://server",
                system_info={},
                heartbeat_interval_seconds=10,
                stop_event=stop_event,
            )

        self.assertEqual(attempts, [1, 2, 3])

    def test_a_rejected_heartbeat_payload_still_stops_the_loop(self) -> None:
        """Contra-testul: 422 rămâne motiv de oprire, ca înainte."""
        attempts = []

        def refuse(server_url, agent_id, heartbeat_payload, agent_key=None):
            attempts.append(heartbeat_payload["sequence"])
            raise FatalTransportError("HTTP 422")

        with patch.object(agent, "send_heartbeat", refuse):
            agent.heartbeat_loop(
                config=_heartbeat_config(),
                server_url="http://server",
                system_info={},
                heartbeat_interval_seconds=10,
                stop_event=FakeStopEvent(trip_after_waits=5),
            )

        self.assertEqual(attempts, [1])

    def test_the_heartbeat_carries_the_agent_key(self) -> None:
        used: List[Optional[str]] = []

        def capture(server_url, agent_id, heartbeat_payload, agent_key=None):
            used.append(agent_key)
            raise TransportError("stop here")

        credentials = AgentCredentials(
            agent_key_path=Path(tempfile.gettempdir()) / "unused-key",
            enrollment_secret_path=Path(tempfile.gettempdir()) / "unused-secret",
        )
        credentials._agent_key = "the-key"

        with patch.object(agent, "send_heartbeat", capture):
            agent.heartbeat_loop(
                config=_heartbeat_config(),
                server_url="http://server",
                system_info={},
                heartbeat_interval_seconds=10,
                stop_event=FakeStopEvent(trip_after_waits=1),
                credentials=credentials,
            )

        self.assertEqual(used, ["the-key"])


if __name__ == "__main__":
    unittest.main()
