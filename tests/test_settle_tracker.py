"""
Teste pentru SettleTracker (Pasul 0.2).

Mecanismul este pură logică de timp, cu ceasuri injectate: niciun test nu
atinge discul și niciunul nu așteaptă timp real.
"""

import os
import unittest

from services.file_monitor import build_file_event_payload
from services.settle_tracker import SettleTracker


class FakeClock:
    """Ceas monoton controlat manual."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWallClock:
    """Ceas de perete controlat manual, pentru occurred_at."""

    def __init__(self):
        self.ticks = 0

    def __call__(self) -> str:
        self.ticks += 1
        return f"2026-01-01T00:00:{self.ticks:02d}+00:00"


class SettleTrackerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.wall_clock = FakeWallClock()
        self.tracker = SettleTracker(
            quiet_seconds=1.0,
            max_wait_seconds=10.0,
            clock=self.clock,
            wall_clock=self.wall_clock,
        )

    def test_nothing_is_due_before_the_quiet_period_elapses(self):
        self.tracker.observe("/tmp/a.exe", "file_created")

        self.clock.advance(0.9)

        self.assertEqual(self.tracker.due(), [])
        self.assertEqual(self.tracker.pending_count(), 1)

    def test_a_single_write_is_released_once_after_the_quiet_period(self):
        self.tracker.observe("/tmp/a.exe", "file_created")

        self.clock.advance(1.0)
        released = self.tracker.due()

        self.assertEqual(len(released), 1)
        self.assertEqual(released[0].path, os.path.abspath("/tmp/a.exe"))
        self.assertEqual(released[0].event_type, "file_created")
        self.assertIsNone(released[0].forced_reason)
        self.assertEqual(self.tracker.pending_count(), 0)

    def test_continuous_writes_are_released_only_after_they_stop(self):
        """
        Bug-ul care a motivat tot pasul.

        EventDebouncer rearma fereastra la fiecare apel, inclusiv pe calea
        suprimată, deci un fișier scris continuu nu era raportat NICIODATĂ —
        și evenimentul pierdut era exact ultimul, singurul în care fișierul e
        complet. Aici, scrierile dese doar prelungesc așteptarea.
        """
        self.tracker.observe("/tmp/big.iso", "file_created")

        # Scrierile trebuie să se oprească înainte de plafon, altfel testul ar
        # verifica din greșeală plafonul (acoperit separat mai jos) în loc de
        # stabilizarea naturală.
        for _ in range(10):
            self.clock.advance(0.5)
            self.tracker.observe("/tmp/big.iso", "file_modified")
            self.assertEqual(self.tracker.due(), [])

        self.clock.advance(1.0)
        released = self.tracker.due()

        self.assertEqual(len(released), 1)
        self.assertEqual(released[0].observation_count, 11)
        self.assertIsNone(released[0].forced_reason)

    def test_endless_writes_are_released_at_the_ceiling(self):
        """
        Un fișier care nu se liniștește niciodată (un log activ) trebuie totuși
        raportat. Fără plafon, mecanismul ar suprima pentru totdeauna — același
        bug, în haine noi.
        """
        self.tracker.observe("/tmp/app.log", "file_created")

        released = []
        for _ in range(40):
            self.clock.advance(0.5)
            self.tracker.observe("/tmp/app.log", "file_modified")
            released.extend(self.tracker.due())

        self.assertEqual(len(released), 1)
        self.assertEqual(released[0].forced_reason, "ceiling")

    def test_event_type_is_frozen_at_the_first_observation(self):
        """Decizia 1: eticheta o dă observația care a deschis fereastra."""
        self.tracker.observe("/tmp/a.exe", "file_created")
        self.clock.advance(0.2)
        self.tracker.observe("/tmp/a.exe", "file_modified")

        self.clock.advance(1.0)
        released = self.tracker.due()

        self.assertEqual(len(released), 1)
        self.assertEqual(released[0].event_type, "file_created")

    def test_a_file_touched_again_after_release_opens_a_new_entry(self):
        self.tracker.observe("/tmp/a.exe", "file_created")
        self.clock.advance(1.0)
        self.tracker.due()

        self.tracker.observe("/tmp/a.exe", "file_modified")
        self.clock.advance(1.0)
        released = self.tracker.due()

        self.assertEqual(len(released), 1)
        self.assertEqual(released[0].event_type, "file_modified")

    def test_distinct_paths_are_tracked_independently(self):
        self.tracker.observe("/tmp/a.exe", "file_created")
        self.clock.advance(0.6)
        self.tracker.observe("/tmp/b.exe", "file_created")

        self.clock.advance(0.5)
        released = self.tracker.due()

        self.assertEqual([entry.path for entry in released],
                         [os.path.abspath("/tmp/a.exe")])
        self.assertEqual(self.tracker.pending_count(), 1)

    def test_due_never_returns_the_same_entry_twice(self):
        self.tracker.observe("/tmp/a.exe", "file_created")
        self.clock.advance(1.0)

        self.assertEqual(len(self.tracker.due()), 1)
        self.assertEqual(self.tracker.due(), [])

    def test_capacity_overflow_releases_early_instead_of_dropping(self):
        """
        Diferența esențială față de EventDebouncer: acolo, depășirea capacității
        arunca intrarea (cel mult un duplicat raportat). Aici aruncarea ar
        însemna un eveniment PIERDUT, deci intrarea se eliberează forțat.
        """
        tracker = SettleTracker(
            quiet_seconds=1.0,
            max_wait_seconds=10.0,
            max_pending_files=3,
            clock=self.clock,
            wall_clock=self.wall_clock,
        )

        for index in range(5):
            tracker.observe(f"/tmp/file_{index}.exe", "file_created")

        released = tracker.due()

        self.assertEqual(len(released), 2)
        self.assertEqual([entry.forced_reason for entry in released],
                         ["capacity", "capacity"])
        self.assertEqual(
            [entry.path for entry in released],
            [os.path.abspath("/tmp/file_0.exe"), os.path.abspath("/tmp/file_1.exe")],
            "La presiune trebuie eliberate intrările care au așteptat cel mai "
            "mult, deci ordinea este cea de intrare — nu 'ultima apariție', "
            "cum era la EventDebouncer.",
        )
        self.assertEqual(tracker.pending_count(), 3)

    def test_flush_releases_everything_pending(self):
        self.tracker.observe("/tmp/a.exe", "file_created")
        self.tracker.observe("/tmp/b.exe", "file_created")

        released = self.tracker.flush()

        self.assertEqual(len(released), 2)
        self.assertTrue(all(e.forced_reason == "shutdown" for e in released))
        self.assertEqual(self.tracker.pending_count(), 0)

    def test_flush_also_drains_entries_already_decided(self):
        tracker = SettleTracker(
            quiet_seconds=1.0,
            max_pending_files=1,
            clock=self.clock,
            wall_clock=self.wall_clock,
        )

        tracker.observe("/tmp/a.exe", "file_created")
        tracker.observe("/tmp/b.exe", "file_created")

        released = tracker.flush()

        self.assertEqual(len(released), 2)


class OccurredAtTests(unittest.TestCase):
    """Decizia 1: occurred_at este prima observație, nu momentul emiterii."""

    def setUp(self):
        self.clock = FakeClock()
        self.wall_clock = FakeWallClock()
        self.tracker = SettleTracker(
            quiet_seconds=1.0,
            clock=self.clock,
            wall_clock=self.wall_clock,
        )

    def test_occurred_at_is_captured_at_the_first_observation(self):
        self.tracker.observe("/tmp/a.exe", "file_created")
        first_timestamp = self.wall_clock.ticks

        self.clock.advance(0.5)
        self.tracker.observe("/tmp/a.exe", "file_modified")

        self.clock.advance(1.0)
        released = self.tracker.due()

        self.assertEqual(released[0].occurred_at, "2026-01-01T00:00:01+00:00")
        self.assertEqual(
            self.wall_clock.ticks,
            first_timestamp,
            "Ceasul de perete nu trebuie consultat decât o dată, la deschiderea "
            "intrării: altfel occurred_at ar aluneca spre momentul emiterii.",
        )


class SettleWaitTests(unittest.TestCase):
    """Decizia 2: settle_wait_ms se populează începând cu Pasul 0.2."""

    def setUp(self):
        self.clock = FakeClock()
        self.tracker = SettleTracker(
            quiet_seconds=1.0,
            clock=self.clock,
            wall_clock=FakeWallClock(),
        )

    def test_settle_wait_is_none_while_pending(self):
        self.tracker.observe("/tmp/a.exe", "file_created")

        with self.tracker._lock:
            pending = next(iter(self.tracker._pending.values()))

        self.assertIsNone(pending.settle_wait_ms)

    def test_settle_wait_spans_first_observation_to_release(self):
        self.tracker.observe("/tmp/a.exe", "file_created")

        self.clock.advance(0.5)
        self.tracker.observe("/tmp/a.exe", "file_modified")

        self.clock.advance(1.0)
        released = self.tracker.due()

        self.assertEqual(
            released[0].settle_wait_ms,
            1500,
            "Este latența totală introdusă (prima observație -> eliberare), "
            "nu durata scrierii (care ar fi fost 500 ms).",
        )


class BuildFileEventPayloadTests(unittest.TestCase):
    def test_defaults_keep_the_previous_shape(self):
        payload = build_file_event_payload(
            agent_id="a1",
            event_type="file_created",
            file_path="/tmp/a.exe",
            agent_instance_id="i1",
        )

        self.assertNotIn("measurements", payload)
        self.assertIn("occurred_at", payload)

    def test_supplied_occurred_at_is_used_verbatim(self):
        payload = build_file_event_payload(
            agent_id="a1",
            event_type="file_created",
            file_path="/tmp/a.exe",
            agent_instance_id="i1",
            occurred_at="2026-01-01T00:00:01+00:00",
        )

        self.assertEqual(payload["occurred_at"], "2026-01-01T00:00:01+00:00")
        self.assertIn("2026-01-01T00:00:01+00:00", payload["description"])

    def test_settle_wait_travels_under_measurements(self):
        payload = build_file_event_payload(
            agent_id="a1",
            event_type="file_created",
            file_path="/tmp/a.exe",
            agent_instance_id="i1",
            settle_wait_ms=1500,
        )

        self.assertEqual(payload["measurements"], {"settle_wait_ms": 1500})
        self.assertNotIn("settle_wait_ms", payload)

    def test_measurements_is_absent_rather_than_null_when_unknown(self):
        """
        Un câmp absent este valid în contract; unul prezent și gol ar fi zgomot
        pe fir pentru fiecare eveniment.
        """
        payload = build_file_event_payload(
            agent_id="a1",
            event_type="file_created",
            file_path="/tmp/a.exe",
            agent_instance_id="i1",
            settle_wait_ms=None,
        )

        self.assertNotIn("measurements", payload)


if __name__ == "__main__":
    unittest.main()