"""
Teste pentru câmpul monitoring din heartbeat (contract v10, "partial" din v11).

Problema închisă: dacă spool-ul nu se deschide, dacă FileMonitor nu pornește
sau dacă un fir al pipeline-ului moare după pornire, agentul rămâne "online" pe
server fără să supravegheze nimic, iar serverul nu află. Heartbeat-ul e singurul
canal care merge în toate trei cazurile, deci starea pleacă pe el, recalculată
la fiecare heartbeat.

v11 închide al patrulea caz: din mai multe directoare configurate doar unele
există. FileMonitor pornea, iar agentul raporta "active" peste o zonă
supravegheată doar în parte.
"""

import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from typing import Any
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
    def __init__(self, alive: bool = True, complete: bool = True):
        self.alive = alive
        self.complete = complete

    def pipeline_alive(self) -> bool:
        return self.alive

    def covers_all_directories(self) -> bool:
        return self.complete


def _assess(event_spool: Any, file_monitor: Any, event_dispatcher: Any) -> str:
    """
    assess_monitoring cu dubluri în locul tipurilor reale.

    Funcția atinge doar „is None", pipeline_alive(), covers_all_directories()
    și is_alive(), deci dublurile de mai sus ajung. Tipurile ei cer însă
    EventSpool, FileMonitor și EventDispatcher; Any le lasă să treacă într-un
    singur loc, nu cu câte un type: ignore la fiecare apel.
    """
    return agent.assess_monitoring(event_spool, file_monitor, event_dispatcher)


class AssessMonitoringTests(unittest.TestCase):
    def test_a_healthy_pipeline_is_active(self) -> None:
        self.assertEqual(
            _assess(object(), _Monitor(), _Alive()), "active"
        )

    def test_no_spool_means_spool_unavailable(self) -> None:
        self.assertEqual(
            _assess(None, None, None), "spool_unavailable"
        )

    def test_no_monitor_means_monitor_unavailable(self) -> None:
        self.assertEqual(
            _assess(object(), None, _Alive()), "monitor_unavailable"
        )

    def test_a_dead_pipeline_thread_means_stopped(self) -> None:
        self.assertEqual(
            _assess(object(), _Monitor(alive=False), _Alive()),
            "stopped",
        )

    def test_a_dead_dispatcher_means_stopped(self) -> None:
        """Evenimentele se strâng în spool și nu mai pleacă: serverul e la fel de orb."""
        self.assertEqual(
            _assess(object(), _Monitor(), _Alive(alive=False)),
            "stopped",
        )

    def test_a_skipped_directory_means_partial(self) -> None:
        self.assertEqual(
            _assess(object(), _Monitor(complete=False), _Alive()),
            "partial",
        )

    def test_a_dead_pipeline_outranks_a_skipped_directory(self) -> None:
        """Un fir mort orbește toată zona, nu doar directorul lipsă."""
        self.assertEqual(
            _assess(
                object(), _Monitor(alive=False, complete=False), _Alive()
            ),
            "stopped",
        )


class DirectoryCoverageTests(unittest.TestCase):
    """Pe FileMonitor real: ce directoare a programat start()."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.missing = os.path.join(self.directory.name, "does-not-exist")
        self.monitor = None

    def tearDown(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor.join(timeout=5)
        self.directory.cleanup()

    def _start(self, directories) -> FileMonitor:
        self.monitor = FileMonitor(
            agent_id="agent-test",
            agent_instance_id="11111111-2222-3333-4444-555555555555",
            monitored_directories=directories,
            recursive_monitoring=False,
            event_callback=lambda payload: None,
            logger=_SILENT,
        )
        self.monitor.start()
        return self.monitor

    def test_every_directory_present_covers_all(self) -> None:
        monitor = self._start([self.directory.name])

        self.assertTrue(monitor.covers_all_directories())
        self.assertEqual(monitor.skipped_directories, [])

    def test_a_missing_directory_is_recorded_and_the_monitor_still_starts(self) -> None:
        monitor = self._start([self.directory.name, self.missing])

        self.assertTrue(monitor.pipeline_alive())
        self.assertFalse(monitor.covers_all_directories())
        self.assertEqual(monitor.skipped_directories, [self.missing])

    def test_a_file_in_place_of_a_directory_is_recorded(self) -> None:
        not_a_directory = os.path.join(self.directory.name, "file.txt")
        with open(not_a_directory, "w", encoding="utf-8") as handle:
            handle.write("x")

        monitor = self._start([self.directory.name, not_a_directory])

        self.assertEqual(monitor.skipped_directories, [not_a_directory])

    def test_the_real_monitor_reports_partial_on_the_heartbeat(self) -> None:
        """Cazul găsit la 21 octombrie 2026, cap la cap pe obiectul real."""
        monitor = self._start([self.directory.name, self.missing])

        self.assertEqual(
            _assess(object(), monitor, _Alive()), "partial"
        )


class LostDirectoryTests(unittest.TestCase):
    """
    Pe FileMonitor real și pe disc real: directorul dispare DUPĂ pornire.

    watchdog oprește doar emitter-ul directorului șters, iar observer-ul
    rămâne viu. Înainte de v11 complet, pipeline_alive() rămânea True și
    heartbeat-ul raporta "active" peste un director pe care nu-l mai vedea.
    """

    def setUp(self) -> None:
        self.root = tempfile.TemporaryDirectory()
        self.kept = os.path.join(self.root.name, "kept")
        self.doomed = os.path.join(self.root.name, "doomed")
        os.mkdir(self.kept)
        os.mkdir(self.doomed)
        self.monitor = FileMonitor(
            agent_id="agent-test",
            agent_instance_id="11111111-2222-3333-4444-555555555555",
            monitored_directories=[self.kept, self.doomed],
            recursive_monitoring=False,
            event_callback=lambda payload: None,
            logger=_SILENT,
        )
        self.monitor.start()

    def tearDown(self) -> None:
        self.monitor.stop()
        self.monitor.join(timeout=5)
        self.root.cleanup()

    def _wait_until_lost(self, directory: str, timeout: float = 5.0) -> list:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lost = self.monitor.lost_directories()
            if directory in lost:
                return lost
            time.sleep(0.05)
        return self.monitor.lost_directories()

    def test_a_healthy_start_loses_nothing(self) -> None:
        self.assertEqual(self.monitor.lost_directories(), [])
        self.assertEqual(
            _assess(object(), self.monitor, _Alive()), "active"
        )

    def test_deleting_a_watched_directory_turns_the_heartbeat_partial(self) -> None:
        shutil.rmtree(self.doomed)

        self.assertEqual(self._wait_until_lost(self.doomed), [self.doomed])
        # Observer-ul trăiește: fără verificarea nouă, starea ar fi "active".
        self.assertTrue(self.monitor.pipeline_alive())
        self.assertEqual(
            _assess(object(), self.monitor, _Alive()), "partial"
        )

    def test_the_emitter_of_a_deleted_directory_stops(self) -> None:
        """Semnalul de bază din watchdog, verificat separat de calea de pe disc."""
        emitter = self.monitor._emitter_for_directory[self.doomed]

        shutil.rmtree(self.doomed)

        emitter.join(timeout=5)
        self.assertFalse(emitter.is_alive())

    def test_a_directory_recreated_at_the_same_path_stays_lost(self) -> None:
        """Emitter-ul oprit nu revine: zona rămâne parțială până la repornire."""
        shutil.rmtree(self.doomed)
        self._wait_until_lost(self.doomed)

        os.mkdir(self.doomed)

        self.assertIn(self.doomed, self.monitor.lost_directories())

    def test_moving_a_watched_directory_away_turns_the_heartbeat_partial(self) -> None:
        os.rename(self.doomed, os.path.join(self.root.name, "moved"))

        self.assertEqual(self._wait_until_lost(self.doomed), [self.doomed])
        self.assertEqual(
            _assess(object(), self.monitor, _Alive()), "partial"
        )

    def test_the_loss_is_logged_once_not_at_every_heartbeat(self) -> None:
        shutil.rmtree(self.doomed)
        self._wait_until_lost(self.doomed)

        with patch.object(self.monitor.logger, "warning") as warning:
            for _ in range(3):
                self.monitor.lost_directories()

        warning.assert_not_called()


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
