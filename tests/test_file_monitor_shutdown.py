"""
Teste pentru secvența de oprire a lui FileMonitor.
===================================================

Miza e semantica parametrului `timeout`. Cu trei etaje de fire, același timeout
dat fiecărei așteptări face ca join(timeout=5) să dureze 15 secunde: semnătura
promite o limită și livrează un multiplu al ei, iar minciuna crește tăcut cu
fiecare etaj adăugat. Testele de aici fixează bugetul ca TOTAL.

A doua miză e ce află apelantul. agent.py închide spool-ul după join(), iar
close() închide conexiunea SQLite fără să întrebe dacă mai are utilizatori. Un
fir de hashing încă viu pierde atunci toată coada rămasă, câte un ERROR pe rând
— deci join() trebuie să spună dacă oprirea chiar s-a terminat.

Etajele sunt înlocuite cu dubluri: aici se testează aritmetica termenului și
propagarea lui, nu hashing-ul.
"""

import logging
import unittest
from typing import List, NamedTuple, Optional, Tuple

from services.file_monitor import FileMonitor


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeStage:
    """
    Un etaj de oprire care consumă timp din bugetul comun când e așteptat.

    Reține timeout-ul primit — de el depinde tot testul: un buget total dă
    valori descrescătoare, unul per etaj le-ar da identice.
    """

    def __init__(
        self,
        clock: FakeClock,
        consumes: float = 0.0,
        alive_after_join: bool = False,
    ):
        self.clock = clock
        self.consumes = consumes
        self.timeouts: List[Optional[float]] = []
        self.stop_deadline: Optional[float] = None
        self.stopped = False
        self._alive_after_join = alive_after_join

    def stop(self, deadline: Optional[float] = None) -> None:
        self.stopped = True
        self.stop_deadline = deadline

    def join(self, timeout: Optional[float] = None) -> None:
        self.timeouts.append(timeout)
        self.clock.advance(self.consumes)

    def is_alive(self) -> bool:
        return self._alive_after_join


class Stages(NamedTuple):
    """
    Referințele către dubluri, ținute separat de monitor.

    Aserțiunile se fac pe ele, nu pe monitor.observer / .hasher: acolo tipul
    declarat e piesa reală, iar un verificator de tipuri are dreptate să
    respingă citirea unui atribut care există doar pe dublură.
    """

    observer: FakeStage
    releaser: FakeStage
    hasher: FakeStage


def _make_monitor(clock: FakeClock, **stage_kwargs) -> Tuple[FileMonitor, Stages]:
    """
    Un FileMonitor cu etajele înlocuite.

    __init__ nu atinge discul și nu pornește niciun fir, deci construcția e
    inofensivă; înlocuim apoi cele trei piese și declarăm monitorul pornit.
    """
    monitor = FileMonitor(
        agent_id="agent-test",
        agent_instance_id="incarnation-test",
        monitored_directories=[],
        recursive_monitoring=False,
        event_callback=lambda payload: None,
        logger=logging.getLogger("test.monitor"),
        clock=clock,
    )

    stages = Stages(
        observer=FakeStage(clock, **stage_kwargs.get("observer", {})),
        releaser=FakeStage(clock, **stage_kwargs.get("releaser", {})),
        hasher=FakeStage(clock, **stage_kwargs.get("hasher", {})),
    )

    monitor.observer = stages.observer  # type: ignore[assignment]
    monitor.releaser = stages.releaser  # type: ignore[assignment]
    monitor.hasher = stages.hasher  # type: ignore[assignment]
    monitor._started = True

    return monitor, stages


class TotalBudgetTests(unittest.TestCase):
    def test_the_timeout_is_a_total_not_a_per_stage_allowance(self) -> None:
        clock = FakeClock()
        monitor, stages = _make_monitor(
            clock,
            observer={"consumes": 2.0},
            releaser={"consumes": 2.0},
        )

        self.assertTrue(monitor.join(timeout=5.0))

        self.assertEqual(stages.observer.timeouts, [5.0])
        self.assertEqual(
            stages.releaser.timeouts,
            [3.0],
            "Releaser-ul a primit bugetul întreg din nou, nu ce a mai rămas.",
        )
        self.assertEqual(stages.hasher.timeouts, [1.0])

    def test_the_hashing_deadline_reserves_time_for_reporting(self) -> None:
        """
        Termenul de hashing trebuie să fie STRICT înaintea termenului total.
        Coincidente, hashing-ul s-ar opri exact când join() cedează, iar
        raportarea ar cădea în intervalul în care agentul deja închide spool-ul.
        """
        clock = FakeClock()
        monitor, stages = _make_monitor(clock)
        monitor.report_reserve_seconds = 1.0

        monitor.join(timeout=5.0)

        # Termen total: 1000 + 5 = 1005. Hashing-ul se oprește cu o secundă
        # mai devreme, ca să apuce să emită.
        self.assertEqual(stages.hasher.stop_deadline, 1004.0)

    def test_an_exhausted_budget_leaves_zero_not_a_negative_wait(self) -> None:
        clock = FakeClock()
        monitor, stages = _make_monitor(clock, observer={"consumes": 9.0})

        monitor.join(timeout=5.0)

        self.assertEqual(stages.releaser.timeouts, [0.0])
        self.assertEqual(stages.hasher.timeouts, [0.0])

        # Termenul de hashing e deja în trecut: nu se mai calculează niciun
        # hash, dar drenarea tot raportează — vezi FileHasher.drain_for_shutdown.
        hash_deadline = stages.hasher.stop_deadline
        self.assertIsNotNone(hash_deadline)
        assert hash_deadline is not None  # pentru verificatorul de tipuri
        self.assertLess(hash_deadline, clock.now)

    def test_without_a_timeout_every_stage_waits_indefinitely(self) -> None:
        clock = FakeClock()
        monitor, stages = _make_monitor(clock)

        monitor.join(timeout=None)

        self.assertEqual(stages.observer.timeouts, [None])
        self.assertEqual(stages.releaser.timeouts, [None])
        self.assertEqual(stages.hasher.timeouts, [None])
        self.assertIsNone(stages.hasher.stop_deadline)


class StallReportingTests(unittest.TestCase):
    """
    Ce află apelantul. De asta depinde dacă agent.py închide spool-ul sub un
    fir încă viu sau nu.
    """

    def test_join_reports_success_when_every_stage_finished(self) -> None:
        clock = FakeClock()
        monitor, stages = _make_monitor(clock)

        self.assertTrue(monitor.join(timeout=5.0))
        self.assertFalse(monitor.is_running())

    def test_a_stalled_hasher_is_reported_and_named(self) -> None:
        clock = FakeClock()
        monitor, stages = _make_monitor(clock, hasher={"alive_after_join": True})

        with self.assertLogs("test.monitor", level="ERROR") as captured:
            self.assertFalse(
                monitor.join(timeout=5.0),
                "join() a raportat oprire completă cu un fir încă viu.",
            )

        self.assertIn("file hasher", captured.output[0])

    def test_a_stalled_observer_is_reported_too(self) -> None:
        clock = FakeClock()
        monitor, stages = _make_monitor(clock, observer={"alive_after_join": True})

        with self.assertLogs("test.monitor", level="ERROR"):
            self.assertFalse(monitor.join(timeout=5.0))

    def test_joining_a_monitor_that_never_started_is_a_success(self) -> None:
        clock = FakeClock()
        monitor, stages = _make_monitor(clock)
        monitor._started = False

        self.assertTrue(monitor.join(timeout=5.0))
        self.assertEqual(stages.hasher.timeouts, [])


if __name__ == "__main__":
    unittest.main()
