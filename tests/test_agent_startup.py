"""
Teste pentru secvența de startup a agentului.

De ce există acest fișier:
    Garanția centrală a agentului într-un deployment izolat este că un endpoint
    continuă să *observe* chiar și atunci când nu poate raporta. Serverul
    inaccesibil la pornire (endpoint pornit înaintea serverului, rețea izolată,
    mentenanță) este o stare tranzitorie normală, nu un motiv de oprire.

    Această garanție se pierde tăcut: dacă register_agent_with_retry abandonează
    după un număr fix de încercări, run_agent() sare peste heartbeat_loop, cade
    în blocul finally și oprește file monitor-ul, dispatcher-ul și spool-ul.
    Procesul iese, iar endpoint-ul rămâne complet nemonitorizat — fără nicio
    eroare vizibilă, doar un agent care „s-a oprit normal" după ~11 minute.

    Un astfel de abandon a supraviețuit unui commit care intenționa exact
    opusul (d495321): pragul a fost redenumit din max_retries în
    warn_after_retries și logul coborât la warning, dar `return False` de sub el
    a rămas pe loc — deci comportamentul nu s-a schimbat deloc. Testele de mai
    jos fixează comportamentul, nu doar numele parametrilor.
"""

from typing import Optional, List
import unittest
from unittest.mock import Mock, patch

import agent
from services.transport import FatalTransportError, TransportError


SERVER_URL = "http://127.0.0.1:8000"


def _make_config() -> dict:
    return {
        "agent_id": "agent-test",
        "agent_instance_id": "11111111-2222-3333-4444-555555555555",
        "agent_version": "1.0.0",
        "server_url": SERVER_URL,
        "heartbeat_interval_seconds": 10,
        "monitored_directories": ["C:/tmp"],
        "recursive_monitoring": True,
    }


class FakeStopEvent:
    """
    Duck-type de threading.Event care nu doarme niciodată.

    Buclele de retry ale agentului folosesc stop_event.wait(timeout=delay) în
    loc de time.sleep(), tocmai ca să rămână responsive la oprire. Testele
    exploatează acest lucru: înlocuind Event-ul real, un backoff de 5–60s
    devine instantaneu, iar numărul de așteptări devine observabil.

    Cu trip_after_waits=N, evenimentul se auto-setează la a N-a așteptare —
    echivalentul unui operator care oprește agentul după N reîncercări.
    """

    def __init__(self, trip_after_waits: Optional[int] = None) -> None:
        self._is_set = False
        self._trip_after_waits = trip_after_waits
        self.waits: List[Optional[float]] = []

    def is_set(self) -> bool:
        return self._is_set

    def set(self) -> None:
        self._is_set = True

    def clear(self) -> None:
        self._is_set = False

    def wait(self, timeout: Optional[float] = None) -> bool:
        self.waits.append(timeout)
        if (
            self._trip_after_waits is not None
            and len(self.waits) >= self._trip_after_waits
        ):
            self._is_set = True
        return self._is_set


def _threshold_warnings(logger_mock: Mock) -> list:
    """Avertismentele de prag, separate de cele emise de controlerul de backoff."""
    return [
        call
        for call in logger_mock.warning.call_args_list
        if "failed to register" in str(call)
    ]


class RegisterAgentWithRetryTests(unittest.TestCase):
    """Bucla de startup renunță doar la eroare permanentă sau la cerere de oprire."""

    def test_keeps_retrying_past_the_warning_threshold(self) -> None:
        """
        Pragul avertizează, nu abandonează.

        Regresia pe care o prinde: un `return False` sub avertisment oprea bucla
        la a warn_after_retries-a încercare. Aici serverul nu răspunde niciodată,
        iar bucla trebuie să depășească pragul de câteva ori.
        """
        attempts = []
        stop_event = FakeStopEvent(trip_after_waits=40)

        def never_answers(server_url):
            attempts.append(server_url)
            raise TransportError("connection refused")

        logger_mock = Mock()

        with patch.object(agent, "check_server_health", never_answers), \
                patch.object(agent, "register_agent") as register_mock, \
                patch.object(agent, "logger", logger_mock):
            registered = agent.register_agent_with_retry(
                config=_make_config(),
                server_url=SERVER_URL,
                system_info={},
                stop_event=stop_event,
                warn_after_retries=5,
            )

        self.assertFalse(registered)
        self.assertEqual(
            len(attempts),
            40,
            "Bucla de startup a abandonat înainte ca oprirea să fie cerută — "
            "endpoint-ul rămâne nemonitorizat cât timp serverul este jos.",
        )
        register_mock.assert_not_called()

    def test_warns_once_at_the_threshold_and_carries_on(self) -> None:
        """Avertismentul de posibilă configurare greșită nu devine spam în log."""
        stop_event = FakeStopEvent(trip_after_waits=30)
        logger_mock = Mock()

        with patch.object(
            agent, "check_server_health", side_effect=TransportError("down")
        ), patch.object(agent, "logger", logger_mock):
            agent.register_agent_with_retry(
                config=_make_config(),
                server_url=SERVER_URL,
                system_info={},
                stop_event=stop_event,
                warn_after_retries=5,
            )

        self.assertEqual(len(_threshold_warnings(logger_mock)), 1)

    def test_returns_only_when_stop_is_requested(self) -> None:
        """Ieșirea din buclă este cauzată de oprire, nu de numărul de încercări."""
        stop_event = FakeStopEvent(trip_after_waits=12)

        with patch.object(
            agent, "check_server_health", side_effect=TransportError("down")
        ), patch.object(agent, "logger"):
            registered = agent.register_agent_with_retry(
                config=_make_config(),
                server_url=SERVER_URL,
                system_info={},
                stop_event=stop_event,
                warn_after_retries=3,
            )

        self.assertFalse(registered)
        self.assertTrue(stop_event.is_set())

    def test_registers_when_the_server_finally_comes_back(self) -> None:
        """Un server care revine după pragul de avertizare este tot acceptat."""
        stop_event = FakeStopEvent(trip_after_waits=100)
        attempts = {"count": 0}

        def answers_on_the_twentieth(server_url):
            attempts["count"] += 1
            if attempts["count"] < 20:
                raise TransportError("connection refused")
            return {"status": "ok"}

        with patch.object(agent, "check_server_health", answers_on_the_twentieth), \
                patch.object(agent, "register_agent", return_value={"status": "ok"}), \
                patch.object(agent, "logger"):
            registered = agent.register_agent_with_retry(
                config=_make_config(),
                server_url=SERVER_URL,
                system_info={},
                stop_event=stop_event,
                warn_after_retries=5,
            )

        self.assertTrue(registered)
        self.assertEqual(attempts["count"], 20)

    def test_aborts_immediately_on_permanent_error(self) -> None:
        """
        FatalTransportError rămâne singura ieșire prin eroare.

        Reîncercarea nu poate repara o configurare sau o autentificare greșită,
        deci aici abandonul este comportamentul corect — spre deosebire de un
        server pur și simplu inaccesibil.
        """
        stop_event = FakeStopEvent(trip_after_waits=10)

        with patch.object(
            agent,
            "check_server_health",
            side_effect=FatalTransportError("401 Unauthorized"),
        ), patch.object(agent, "logger"):
            registered = agent.register_agent_with_retry(
                config=_make_config(),
                server_url=SERVER_URL,
                system_info={},
                stop_event=stop_event,
            )

        self.assertFalse(registered)
        self.assertEqual(
            stop_event.waits,
            [],
            "O eroare permanentă nu trebuie să aștepte niciun backoff.",
        )


class FakeSpool:
    def __init__(self, journal: list) -> None:
        self._journal = journal
        self.enqueued: list = []
        journal.append("spool_opened")

    def enqueue(self, payload) -> None:
        self.enqueued.append(payload)

    def close(self) -> None:
        self._journal.append("spool_closed")


class FakeDispatcher:
    def __init__(self, journal: list, **kwargs) -> None:
        self._journal = journal

    def start(self) -> None:
        self._journal.append("dispatcher_started")

    def wake(self) -> None:
        pass

    def stop(self) -> None:
        self._journal.append("dispatcher_stopped")

    def join(self, timeout=None) -> None:
        pass


class FakeMonitor:
    def __init__(self, journal: list) -> None:
        self._journal = journal
        self._running = True

    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        self._running = False
        self._journal.append("monitor_stopped")

    def join(self, timeout=None) -> None:
        pass


class _FakeThreadingModule:
    """
    Expune doar Event(), singurul membru al lui threading folosit la runtime
    de agent.py (adnotările de tip sunt deja evaluate la import).

    Se înlocuiește atributul modulului agent, nu modulul threading global,
    ca să nu fie afectate alte thread-uri din procesul de test.
    """

    def __init__(self, event) -> None:
        self._event = event

    def Event(self):
        return self._event


class RunAgentStartupOrderTests(unittest.TestCase):
    """Monitorizarea locală pornește înaintea oricărui contact cu serverul."""

    def _run_agent_with_dead_server(self, health_side_effect, trip_after_waits):
        """
        Rulează run_agent() contra unui server care nu răspunde.

        Doar marginile procesului sunt înlocuite (config, system info, spool,
        dispatcher, monitor, transport). register_agent_with_retry rulează real,
        pentru ca testul să acopere chiar interacțiunea dintre bucla de startup
        și ordinea de pornire din run_agent.
        """
        journal: list = []
        stop_event = FakeStopEvent(trip_after_waits=trip_after_waits)

        def record_health(server_url):
            journal.append("server_contacted")
            return health_side_effect(server_url)

        monitor = FakeMonitor(journal)

        def fake_start_file_monitoring(config, event_callback):
            journal.append("monitor_started")
            return monitor

        with patch.object(agent, "threading", _FakeThreadingModule(stop_event)), \
                patch.object(agent, "signal"), \
                patch.object(agent, "load_config", return_value=_make_config()), \
                patch.object(agent, "collect_system_info", return_value={}), \
                patch.object(
                    agent, "EventSpool", lambda **kwargs: FakeSpool(journal)
                ), \
                patch.object(
                    agent,
                    "EventDispatcher",
                    lambda **kwargs: FakeDispatcher(journal, **kwargs),
                ), \
                patch.object(
                    agent, "start_file_monitoring", fake_start_file_monitoring
                ), \
                patch.object(agent, "check_server_health", record_health), \
                patch.object(agent, "register_agent", return_value={"status": "ok"}), \
                patch.object(agent, "send_event", return_value={"status": "ok"}), \
                patch.object(agent, "heartbeat_loop") as heartbeat_mock, \
                patch.object(agent, "logger"):
            agent.run_agent()

        return journal, heartbeat_mock

    def test_local_monitoring_starts_before_the_server_is_contacted(self) -> None:
        """
        Ordinea este garanția: spool → dispatcher → monitor → abia apoi rețeaua.

        Dacă serverul ar fi contactat primul, orice întârziere a înregistrării
        s-ar traduce direct în evenimente de fișier pierdute.
        """
        journal, _ = self._run_agent_with_dead_server(
            health_side_effect=lambda url: (_ for _ in ()).throw(
                TransportError("connection refused")
            ),
            trip_after_waits=20,
        )

        first_contact = journal.index("server_contacted")
        for step in ("spool_opened", "dispatcher_started", "monitor_started"):
            self.assertIn(step, journal)
            self.assertLess(
                journal.index(step),
                first_contact,
                f"{step} s-a produs după primul contact cu serverul — "
                "evenimentele de dinaintea înregistrării se pierd.",
            )

    def test_monitoring_survives_a_server_that_never_answers(self) -> None:
        """
        Cu serverul jos, agentul continuă să reîncerce în loc să se oprească.

        Regresia pe care o prinde: bucla de startup abandona după prag, ceea ce
        ducea execuția direct în finally — monitor oprit, dispatcher oprit,
        spool închis, proces terminat, endpoint nemonitorizat.
        """
        journal, heartbeat_mock = self._run_agent_with_dead_server(
            health_side_effect=lambda url: (_ for _ in ()).throw(
                TransportError("connection refused")
            ),
            trip_after_waits=25,
        )

        # Comparația se face cu numărul de reîncercări cerut de test, nu cu
        # constanta de prag din agent: un prag redenumit sau mutat nu trebuie
        # să transforme această regresie într-o eroare de atribut.
        contacts = journal.count("server_contacted")
        self.assertEqual(
            contacts,
            25,
            "Agentul a încetat să mai caute serverul înainte de a i se cere "
            "oprirea — endpoint-ul rămâne nemonitorizat.",
        )
        heartbeat_mock.assert_not_called()

        # Oprirea rămâne totuși curată odată ce este cerută explicit.
        self.assertIn("monitor_stopped", journal)
        self.assertIn("dispatcher_stopped", journal)
        self.assertIn("spool_closed", journal)

    def test_startup_event_is_queued_before_the_server_is_reachable(self) -> None:
        """Evenimentul de pornire așteaptă pe disc, nu se pierde."""
        journal: list = []
        stop_event = FakeStopEvent(trip_after_waits=5)
        spools: list = []

        def make_spool(**kwargs):
            spool = FakeSpool(journal)
            spools.append(spool)
            return spool

        with patch.object(agent, "threading", _FakeThreadingModule(stop_event)), \
                patch.object(agent, "signal"), \
                patch.object(agent, "load_config", return_value=_make_config()), \
                patch.object(agent, "collect_system_info", return_value={}), \
                patch.object(agent, "EventSpool", make_spool), \
                patch.object(
                    agent,
                    "EventDispatcher",
                    lambda **kwargs: FakeDispatcher(journal, **kwargs),
                ), \
                patch.object(
                    agent, "start_file_monitoring", lambda config, cb: FakeMonitor(journal)
                ), \
                patch.object(
                    agent,
                    "check_server_health",
                    side_effect=TransportError("connection refused"),
                ), \
                patch.object(agent, "send_event") as send_event_mock, \
                patch.object(agent, "heartbeat_loop"), \
                patch.object(agent, "logger"):
            agent.run_agent()

        self.assertEqual(len(spools), 1)
        queued_types = [event.get("event_type") for event in spools[0].enqueued]
        self.assertEqual(queued_types, ["agent_startup"])

        # Nimic nu se trimite direct cât timp coada persistentă este disponibilă,
        # iar shutdown-ul nu se raportează pentru un agent neînregistrat.
        send_event_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()