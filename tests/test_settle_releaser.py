"""
Teste pentru SettleReleaser: puntea dintre tracker și spool.
=============================================================

Miza acestor teste este consecința mutării emiterii de pe firul watchdog pe
firul releaser-ului. În vechiul flux, un eșec al callback-ului era recuperabil
de la sine — următoarea scriere regenera evenimentul. Aici, due() scoate
intrarea din tracker, deci un eșec nereținut devine pierdere definitivă:
fișierul s-a liniștit, niciun watchdog nu-l mai reînvie.

De aceea testele acoperă cu precădere căile de eșec: reținerea payload-ului
între treceri, stabilitatea lui client_event_id peste reîncercări (de ea
depinde deduplicarea at-least-once a spool-ului), abandonul zgomotos la
epuizarea încercărilor și semantica „o singură șansă" a drenării de la oprire.

Corpul buclei (release_due_once) se testează direct, fără fir și fără timp
real, cu ceasuri false în tracker. Firul propriu-zis primește un singur test
de viață, cu constante mici.
"""

import logging
import threading
import time
import unittest
from typing import List, Optional

from services.file_monitor import SettleReleaser
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
    """Ceas de perete cu valoare fixă, recognoscibilă în aserțiuni."""

    def __init__(self, value: str = "2026-01-01T00:00:00+00:00"):
        self.value = value

    def __call__(self) -> str:
        return self.value


class FlakyCallback:
    """Callback care eșuează primele fail_times apeluri, apoi reușește.

    Reține fiecare payload primit, inclusiv la apelurile eșuate — exact ce
    trebuie pentru a verifica stabilitatea lui client_event_id.
    """

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls: List[dict] = []

    def __call__(self, payload: dict) -> None:
        self.calls.append(payload)
        if len(self.calls) <= self.fail_times:
            raise OSError("simulated spool failure")


def _make_releaser(
    callback,
    clock: Optional[FakeClock] = None,
    wall_clock: Optional[FakeWallClock] = None,
    quiet_seconds: float = 1.0,
    max_emit_attempts: int = 5,
):
    clock = clock or FakeClock()
    wall_clock = wall_clock or FakeWallClock()

    tracker = SettleTracker(
        quiet_seconds=quiet_seconds,
        clock=clock,
        wall_clock=wall_clock,
        logger=logging.getLogger("test.tracker"),
    )
    releaser = SettleReleaser(
        tracker=tracker,
        event_callback=callback,
        agent_id="agent-test",
        agent_instance_id="incarnation-test",
        logger=logging.getLogger("test.releaser"),
        max_emit_attempts=max_emit_attempts,
    )
    return tracker, releaser, clock, wall_clock


class HappyPathTests(unittest.TestCase):
    """Drumul fără eșecuri: din tracker, prin builder, în callback."""

    def test_a_settled_file_is_emitted_with_its_full_payload(self) -> None:
        callback = FlakyCallback()
        tracker, releaser, clock, wall_clock = _make_releaser(callback)

        tracker.observe("C:/tmp/proba.txt", "file_created")
        clock.advance(1.5)

        emitted = releaser.release_due_once()

        self.assertEqual(emitted, 1)
        self.assertEqual(len(callback.calls), 1)

        payload = callback.calls[0]
        self.assertEqual(payload["event_type"], "file_created")
        self.assertEqual(payload["agent_id"], "agent-test")
        self.assertEqual(payload["agent_instance_id"], "incarnation-test")

        # occurred_at vine din tracker (prima observație), nu din ceasul
        # emiterii — Decizia 1 de la Pasul 0.2.
        self.assertEqual(payload["occurred_at"], wall_clock.value)

        # settle_wait_ms = released_at - first_seen, în milisecunde.
        self.assertEqual(payload["measurements"], {"settle_wait_ms": 1500})

    def test_a_quiet_pass_emits_nothing(self) -> None:
        callback = FlakyCallback()
        tracker, releaser, clock, _ = _make_releaser(callback)

        tracker.observe("C:/tmp/proba.txt", "file_created")
        clock.advance(0.5)

        self.assertEqual(releaser.release_due_once(), 0)
        self.assertEqual(callback.calls, [])
        self.assertEqual(tracker.pending_count(), 1)


class RetryTests(unittest.TestCase):
    """Căile de eșec: exact ce vechiul flux pierdea în tăcere."""

    def test_a_failed_delivery_is_retried_on_the_next_pass(self) -> None:
        callback = FlakyCallback(fail_times=1)
        tracker, releaser, clock, _ = _make_releaser(callback)

        tracker.observe("C:/tmp/proba.txt", "file_created")
        clock.advance(1.5)

        with self.assertLogs("test.releaser", level="WARNING"):
            self.assertEqual(releaser.release_due_once(), 0)

        # Intrarea a părăsit tracker-ul (due() e definitiv), dar trăiește în
        # coada de reîncercare a releaser-ului.
        self.assertEqual(tracker.pending_count(), 0)

        self.assertEqual(releaser.release_due_once(), 1)
        self.assertEqual(len(callback.calls), 2)

    def test_client_event_id_is_stable_across_retries(self) -> None:
        """
        Payload-ul se construiește o singură dată; reîncercarea NU generează
        un client_event_id nou. De asta depinde deduplicarea serverului dacă o
        încercare a apucat să producă efecte parțiale înainte să eșueze.
        """
        callback = FlakyCallback(fail_times=1)
        tracker, releaser, clock, _ = _make_releaser(callback)

        tracker.observe("C:/tmp/proba.txt", "file_created")
        clock.advance(1.5)

        with self.assertLogs("test.releaser", level="WARNING"):
            releaser.release_due_once()
        releaser.release_due_once()

        self.assertEqual(len(callback.calls), 2)
        self.assertEqual(
            callback.calls[0]["client_event_id"],
            callback.calls[1]["client_event_id"],
        )

    def test_retries_run_before_freshly_due_files(self) -> None:
        """Ce a eșuat a așteptat deja o trecere întreagă, deci are prioritate."""
        callback = FlakyCallback(fail_times=1)
        tracker, releaser, clock, _ = _make_releaser(callback)

        tracker.observe("C:/tmp/primul.txt", "file_created")
        clock.advance(1.5)

        with self.assertLogs("test.releaser", level="WARNING"):
            releaser.release_due_once()

        tracker.observe("C:/tmp/al-doilea.txt", "file_created")
        clock.advance(1.5)

        self.assertEqual(releaser.release_due_once(), 2)

        successful = callback.calls[1:]
        self.assertIn("primul.txt", successful[0]["file_path"])
        self.assertIn("al-doilea.txt", successful[1]["file_path"])

    def test_after_the_attempt_ceiling_the_event_is_dropped_with_an_error(
        self,
    ) -> None:
        """Decizia 2: abandon zgomotos, nu blocarea cozii la nesfârșit."""
        callback = FlakyCallback(fail_times=100)
        tracker, releaser, clock, _ = _make_releaser(
            callback, max_emit_attempts=3
        )

        tracker.observe("C:/tmp/proba.txt", "file_created")
        clock.advance(1.5)

        with self.assertLogs("test.releaser", level="WARNING"):
            releaser.release_due_once()
        with self.assertLogs("test.releaser", level="WARNING"):
            releaser.release_due_once()

        with self.assertLogs("test.releaser", level="ERROR"):
            releaser.release_due_once()

        # A patra trecere nu mai are ce încerca: coada e goală.
        self.assertEqual(releaser.release_due_once(), 0)
        self.assertEqual(len(callback.calls), 3)


class ShutdownDrainTests(unittest.TestCase):
    """Drenarea finală: totul iese, fiecare cu o singură șansă."""

    def test_drain_flushes_entries_still_waiting(self) -> None:
        callback = FlakyCallback()
        tracker, releaser, clock, _ = _make_releaser(callback)

        tracker.observe("C:/tmp/proba.txt", "file_created")
        # Fără avans de ceas: fișierul NU s-a stabilizat.

        self.assertEqual(releaser.drain_for_shutdown(), 1)
        self.assertEqual(tracker.pending_count(), 0)

        payload = callback.calls[0]
        self.assertIn("measurements", payload)

    def test_drain_gives_each_failure_a_single_attempt(self) -> None:
        """
        La oprire nu există „treceri următoare", deci un eșec nu se reține:
        se raportează direct la ERROR și se abandonează.
        """
        callback = FlakyCallback(fail_times=100)
        tracker, releaser, clock, _ = _make_releaser(callback)

        tracker.observe("C:/tmp/proba.txt", "file_created")

        with self.assertLogs("test.releaser", level="ERROR"):
            self.assertEqual(releaser.drain_for_shutdown(), 0)

        self.assertEqual(len(callback.calls), 1)
        self.assertEqual(releaser.release_due_once(), 0)

    def test_drain_also_empties_the_retry_queue(self) -> None:
        callback = FlakyCallback(fail_times=1)
        tracker, releaser, clock, _ = _make_releaser(callback)

        tracker.observe("C:/tmp/proba.txt", "file_created")
        clock.advance(1.5)

        with self.assertLogs("test.releaser", level="WARNING"):
            releaser.release_due_once()

        self.assertEqual(releaser.drain_for_shutdown(), 1)
        self.assertEqual(len(callback.calls), 2)


class WorkerThreadTests(unittest.TestCase):
    """Viața firului: cu timp real, dar cu constante mici."""

    def test_the_thread_emits_end_to_end_and_stops_cleanly(self) -> None:
        captured: List[dict] = []
        arrived = threading.Event()

        def callback(payload: dict) -> None:
            captured.append(payload)
            arrived.set()

        tracker = SettleTracker(
            quiet_seconds=0.05, logger=logging.getLogger("test.tracker")
        )
        releaser = SettleReleaser(
            tracker=tracker,
            event_callback=callback,
            agent_id="agent-test",
            agent_instance_id="incarnation-test",
            logger=logging.getLogger("test.releaser"),
            poll_seconds=0.01,
        )

        releaser.start()
        try:
            tracker.observe("C:/tmp/proba.txt", "file_created")
            self.assertTrue(
                arrived.wait(timeout=5.0),
                "Firul releaser-ului nu a emis evenimentul în timp util.",
            )
        finally:
            releaser.stop()
            releaser.join(timeout=5.0)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["agent_instance_id"], "incarnation-test")

    def test_the_loop_survives_an_unexpected_exception(self) -> None:
        """
        Garanția tracker-ului trăiește doar cât trăiește bucla. O excepție
        scăpată din trecere nu are voie să omoare firul — altfel monitorizarea
        orbește în tăcere.
        """
        tracker = SettleTracker(logger=logging.getLogger("test.tracker"))
        releaser = SettleReleaser(
            tracker=tracker,
            event_callback=lambda payload: None,
            agent_id="agent-test",
            agent_instance_id="incarnation-test",
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
            calls["count"],
            2,
            "Bucla trebuia să continue după prima excepție.",
        )


if __name__ == "__main__":
    unittest.main()