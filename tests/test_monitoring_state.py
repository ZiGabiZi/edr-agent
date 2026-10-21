"""
Teste pentru câmpul monitoring din heartbeat (contract v10).

Problema închisă: dacă spool-ul nu se deschide, dacă FileMonitor nu pornește
sau dacă un fir al pipeline-ului moare după pornire, agentul rămâne "online" pe
server fără să supravegheze nimic, iar serverul nu află. Heartbeat-ul e singurul
canal care merge în toate trei cazurile, deci starea pleacă pe el, recalculată
la fiecare heartbeat.
"""

import logging
import tempfile
import threading
import unittest
from unittest.mock import patch

import agent
from services.event_spool import EventSpoolError
from services.file_monitor import FileMonitor
from tests import test_agent_startup as startup
from tests.test_heartbeat_payload import _loop_isolated_from_the_network, _make_config


_SILENT = logging.getLogger("test_monitoring_state")
_SILENT.addHandler(logging.NullHandler())
_SILENT.propagate = False


class _Alive:
    """Dublură pentru spool / dispecer: contează doar is_alive()."""

    def __init__(self, alive: bool = True):
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


class _Monitor:
    def __init__(self, alive: bool = True):
        self.alive = alive

    def pipeline_alive(self) -> bool:
        return self.alive


class AssessMonitoringTests(unittest.TestCase):
    def test_a_healthy_pipeline_is_active(self) -> None:
        self.assertEqual(
            agent.assess_monitoring(object(), _Monitor(), _Alive()), "active"
        )

    def test_no_spool_means_spool_unavailable(self) -> None:
        self.assertEqual(
            agent.assess_monitoring(None, None, None), "spool_unavailable"
        )

    def test_no_monitor_means_monitor_unavailable(self) -> None:
        self.assertEqual(
            agent.assess_monitoring(object(), None, _Alive()), "monitor_unavailable"
        )

    def test_a_dead_pipeline_thread_means_stopped(self) -> None:
        self.assertEqual(
            agent.assess_monitoring(object(), _Monitor(alive=False), _Alive()),
            "stopped",
        )

    def test_a_dead_dispatcher_means_stopped(self) -> None:
        """Evenimentele se strâng în spool și nu mai pleacă: serverul e la fel de orb."""
        self.assertEqual(
            agent.assess_monitoring(object(), _Monitor(), _Alive(alive=False)),
            "stopped",
        )


class PipelineAliveTests(unittest.TestCase):
    """Pe FileMonitor real: is_running() se uita doar la observer."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.monitor = FileMonitor(
            agent_id="agent-test",
            agent_instance_id="11111111-2222-3333-4444-555555555555",
            monitored_directories=[self.directory.name],
            recursive_monitoring=False,
            event_callback=lambda payload: None,
            logger=_SILENT,
        )

    def tearDown(self) -> None:
        self.monitor.stop()
        self.monitor.join(timeout=5)
        self.directory.cleanup()

    def test_a_monitor_that_never_started_is_not_alive(self) -> None:
        self.assertFalse(self.monitor.pipeline_alive())

    def test_a_started_monitor_is_alive(self) -> None:
        self.monitor.start()

        self.assertTrue(self.monitor.pipeline_alive())

    def test_a_dead_releaser_stops_the_pipeline_while_the_observer_runs(self) -> None:
        self.monitor.start()

        self.monitor.releaser.stop()
        self.monitor.releaser.join(timeout=5)

        self.assertTrue(self.monitor.is_running())
        self.assertFalse(self.monitor.pipeline_alive())

    def test_a_dead_observer_stops_the_pipeline(self) -> None:
        self.monitor.start()

        self.monitor.observer.stop()
        self.monitor.observer.join(timeout=5)

        self.assertFalse(self.monitor.pipeline_alive())


class HeartbeatLoopMonitoringTests(unittest.TestCase):
    def _run_loop(self, heartbeats: int, **loop_kwargs) -> list:
        stop_event = threading.Event()
        sent_payloads = []

        def fake_send_heartbeat(server_url, agent_id, heartbeat_payload, agent_key=None):
            sent_payloads.append(dict(heartbeat_payload))
            if len(sent_payloads) == heartbeats:
                stop_event.set()
            return {"status": "ok", "directive": {"action": "none"}}

        with _loop_isolated_from_the_network(fake_send_heartbeat, stop_event):
            agent.heartbeat_loop(
                config=_make_config(),
                server_url="http://127.0.0.1:8000",
                system_info={},
                heartbeat_interval_seconds=0.01,
                stop_event=stop_event,
                **loop_kwargs,
            )

        return sent_payloads

    def test_the_state_is_reassessed_at_every_heartbeat(self) -> None:
        """Moartea în timpul rulării: un semnal trimis doar la pornire n-ar prinde-o."""
        states = iter(["active", "active", "stopped"])

        sent = self._run_loop(3, monitoring_state=lambda: next(states))

        self.assertEqual(
            [payload["monitoring"] for payload in sent],
            ["active", "active", "stopped"],
        )

    def test_a_loop_without_a_pipeline_never_claims_active(self) -> None:
        sent = self._run_loop(1)

        self.assertEqual(sent[0]["monitoring"], "monitor_unavailable")


class RunAgentWiringTests(unittest.TestCase):
    """
    Cele două eșecuri de pornire din run_agent ajung pe heartbeat.

    Înainte, amândouă se terminau cu „continues with heartbeat only" în log și
    nimic pe fir: agentul se înregistra, rămânea "online", iar serverul nu afla.
    """

    def _monitoring_reported_by_run_agent(self, spool_factory, start_monitoring) -> str:
        journal: list = []
        stop_event = startup.FakeStopEvent(trip_after_waits=5)

        with patch.object(agent, "threading", startup._FakeThreadingModule(stop_event)),                 patch.object(agent, "signal"),                 patch.object(agent, "load_config", return_value=startup._make_config()),                 patch.object(agent, "collect_system_info", return_value={}),                 patch.object(agent, "EventSpool", spool_factory(journal)),                 patch.object(
                    agent,
                    "EventDispatcher",
                    lambda **kwargs: startup.FakeDispatcher(journal, **kwargs),
                ),                 patch.object(agent, "start_file_monitoring", start_monitoring),                 patch.object(agent, "check_server_health", return_value={"status": "ok"}),                 patch.object(agent, "register_agent", return_value={"status": "ok"}),                 patch.object(agent, "send_event", return_value={"status": "ok"}),                 patch.object(agent, "heartbeat_loop") as heartbeat_mock,                 patch.object(agent, "logger"):
            agent.run_agent()

        heartbeat_mock.assert_called_once()
        return heartbeat_mock.call_args.kwargs["monitoring_state"]()

    def test_a_spool_that_does_not_open_is_reported(self) -> None:
        def failing_spool(journal):
            def make(**kwargs):
                raise EventSpoolError("disk full")
            return make

        self.assertEqual(
            self._monitoring_reported_by_run_agent(
                failing_spool, lambda config, callback: None
            ),
            "spool_unavailable",
        )

    def test_a_monitor_that_does_not_start_is_reported(self) -> None:
        self.assertEqual(
            self._monitoring_reported_by_run_agent(
                lambda journal: (lambda **kwargs: startup.FakeSpool(journal)),
                lambda config, callback: None,
            ),
            "monitor_unavailable",
        )


if __name__ == "__main__":
    unittest.main()
