"""
Teste pentru SettleReleaser: puntea dintre tracker și coada de hashing.
========================================================================

Rolul releaser-ului s-a îngustat odată cu introducerea firului de hashing: nu
mai construiește payload-uri și nu mai emite nimic. Predă. Reîncercările de
livrare și stabilitatea lui client_event_id au migrat în FileHasher, împreună
cu construcția payload-ului.

Ce rămâne de verificat aici e tocmai îngustimea: fiecare trecere trebuie să fie
în timp constant, indiferent ce se întâmplă în aval. Dacă releaser-ul ar aștepta
vreodată după hasher, garanția tracker-ului („orice intrare iese în cel mult
max_wait_seconds") ar cădea, pentru că nimeni nu i-ar mai apela due().
"""

import logging
import threading
import time
import unittest
from typing import List

from services.file_monitor import SettleReleaser
from services.settle_tracker import PendingFile, SettleTracker


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWallClock:
    def __init__(self, value: str = "2026-01-01T00:00:00+00:00"):
        self.value = value

    def __call__(self) -> str:
        return self.value


class RecordingHasher:
    """Un hasher fals: reține ce i s-a predat, fără să calculeze nimic."""

    def __init__(self):
        self.submitted: List[PendingFile] = []
        self.arrived = threading.Event()

    def submit(self, pending: PendingFile) -> None:
        self.submitted.append(pending)
        self.arrived.set()


def _make_releaser(quiet_seconds: float = 1.0, poll_seconds: float = 0.25):
    clock = FakeClock()
    tracker = SettleTracker(
        quiet_seconds=quiet_seconds,
        clock=clock,
        wall_clock=FakeWallClock(),
        logger=logging.getLogger("test.tracker"),
    )
    hasher = RecordingHasher()
    releaser = SettleReleaser(
        tracker=tracker,
        hasher=hasher,
        logger=logging.getLogger("test.releaser"),
        poll_seconds=poll_seconds,
    )
    return tracker, hasher, releaser, clock


class HandoverTests(unittest.TestCase):
    def test_a_settled_file_is_handed_to_the_hasher(self) -> None:
        tracker, hasher, releaser, clock = _make_releaser()

        tracker.observe("C:/tmp/proba.txt", "file_created")
        clock.advance(1.5)

        self.assertEqual(releaser.release_due_once(), 1)
        self.assertEqual(len(hasher.submitted), 1)

        handed = hasher.submitted[0]
        self.assertEqual(handed.event_type, "file_created")
        self.assertEqual(handed.settle_wait_ms, 1500)

    def test_a_quiet_pass_hands_over_nothing(self) -> None:
        tracker, hasher, releaser, clock = _make_releaser()

        tracker.observe("C:/tmp/proba.txt", "file_created")
        clock.advance(0.5)

        self.assertEqual(releaser.release_due_once(), 0)
        self.assertEqual(hasher.submitted, [])
        self.assertEqual(tracker.pending_count(), 1)

    def test_the_releaser_never_builds_a_payload(self) -> None:
        """
        Regresie asupra separării: dacă construcția payload-ului s-ar întoarce
        aici, hasher-ul ar primi un dicționar în loc de o intrare, iar
        hash_status-ul n-ar mai avea cine să-l pună.
        """
        tracker, hasher, releaser, clock = _make_releaser()

        tracker.observe("C:/tmp/proba.txt", "file_created")
        clock.advance(1.5)
        releaser.release_due_once()

        self.assertIsInstance(hasher.submitted[0], PendingFile)


class ShutdownDrainTests(unittest.TestCase):
    def test_drain_hands_over_everything_still_waiting(self) -> None:
        tracker, hasher, releaser, clock = _make_releaser()

        tracker.observe("C:/tmp/unu.txt", "file_created")
        tracker.observe("C:/tmp/doi.txt", "file_created")
        # Fără avans de ceas: niciunul nu s-a stabilizat.

        self.assertEqual(releaser.drain_for_shutdown(), 2)
        self.assertEqual(len(hasher.submitted), 2)
        self.assertEqual(tracker.pending_count(), 0)

    def test_drained_entries_carry_the_shutdown_reason(self) -> None:
        tracker, hasher, releaser, clock = _make_releaser()

        tracker.observe("C:/tmp/proba.txt", "file_created")
        releaser.drain_for_shutdown()

        self.assertEqual(hasher.submitted[0].forced_reason, "shutdown")


class WorkerThreadTests(unittest.TestCase):
    def test_the_thread_hands_over_end_to_end_and_stops_cleanly(self) -> None:
        tracker = SettleTracker(
            quiet_seconds=0.05, logger=logging.getLogger("test.tracker")
        )
        hasher = RecordingHasher()
        releaser = SettleReleaser(
            tracker=tracker,
            hasher=hasher,
            logger=logging.getLogger("test.releaser"),
            poll_seconds=0.01,
        )

        releaser.start()
        try:
            tracker.observe("C:/tmp/proba.txt", "file_created")
            self.assertTrue(
                hasher.arrived.wait(timeout=5.0),
                "Firul releaser-ului nu a predat intrarea in timp util.",
            )
        finally:
            releaser.stop()
            releaser.join(timeout=5.0)

        self.assertEqual(len(hasher.submitted), 1)

    def test_the_loop_survives_an_unexpected_exception(self) -> None:
        tracker = SettleTracker(logger=logging.getLogger("test.tracker"))
        releaser = SettleReleaser(
            tracker=tracker,
            hasher=RecordingHasher(),
            logger=logging.getLogger("test.releaser"),
            poll_seconds=0.01,
        )

        calls = {"count": 0}

        def exploding_pass() -> int:
            calls["count"] += 1
            raise RuntimeError("simulated pass failure")

        releaser.release_due_once = exploding_pass  # type: ignore[method-assign]

        with self.assertLogs("test.releaser", level="ERROR"):
            releaser.start()
            try:
                deadline = time.monotonic() + 5.0
                while calls["count"] < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                releaser.stop()
                releaser.join(timeout=5.0)

        self.assertGreaterEqual(
            calls["count"], 2, "Bucla trebuia sa continue dupa prima exceptie."
        )


if __name__ == "__main__":
    unittest.main()