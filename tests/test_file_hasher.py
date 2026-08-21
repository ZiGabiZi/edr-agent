"""
Teste pentru FileHasher: treapta T0 și apărarea ei.
====================================================

Miza centrală e verificarea dublă. SettleTracker produce un candidat, nu un
adevăr — watchdog nu garantează un eveniment pentru fiecare scriere. Un hash
calculat pe un fișier încă în scriere descrie o stare care nu a existat
niciodată ca fișier finit, iar serverul l-ar interoga într-o reputație și ar
primi „necunoscut" pentru un fișier care poate fi perfect cunoscut.

Testele care contează cel mai mult sunt de aceea cele în care fișierul se
schimbă între cele două stat-uri: ele verifică nu că hash-ul e corect, ci că un
hash incorect nu ajunge niciodată pe fir. Fereastra are două jumătăți, iar
DoubleCheckTests le acoperă separat pentru că nu sunt la fel:

  - schimbare ÎN TIMPUL citirii — digest-ul iese rupt, o stare care nu a
    existat niciodată ca fișier finit. E cazul pentru care mecanismul există;
  - schimbare ÎNAINTE de citire, între primul stat și deschidere — digest-ul
    descrie de fapt starea nouă, completă. Îl respingem oricum, pentru că din
    ce vede hasher-ul cele două cazuri nu se pot distinge.

Restul acoperă vocabularul de hash_status și granițele: plafonul de dimensiune
verificat fără citire, plafonul de reintroduceri, degradarea la capacitate și
drenarea în limita bugetului de oprire.
"""

import builtins
import contextlib
import hashlib
import logging
import os
import tempfile
import threading
import time
import unittest
from typing import List, Optional

from services.file_hasher import FileHasher
from services.settle_tracker import PendingFile, SettleTracker


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TickingClock:
    """
    Ceas care avansează la FIECARE citire.

    Necesar ca să se poată observa scurgerea timpului în interiorul unei
    singure operații — hash_file citește ceasul o dată per bloc de 1 MiB, iar
    un FakeClock static nu poate arăta niciodată un termen depășit la jumătatea
    unei citiri.
    """

    def __init__(self, step: float = 1.0, start: float = 1000.0):
        self.now = start
        self.step = step

    def __call__(self) -> float:
        current = self.now
        self.now += self.step
        return current


class FlakyCallback:
    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls: List[dict] = []

    def __call__(self, payload: dict) -> None:
        self.calls.append(payload)
        if len(self.calls) <= self.fail_times:
            raise OSError("simulated spool failure")


def _make_hasher(callback, tracker: Optional[SettleTracker] = None, **kwargs):
    tracker = tracker or SettleTracker(logger=logging.getLogger("test.tracker"))
    hasher = FileHasher(
        tracker=tracker,
        event_callback=callback,
        agent_id="agent-test",
        agent_instance_id="incarnation-test",
        logger=logging.getLogger("test.hasher"),
        **kwargs,
    )
    return tracker, hasher


def _pending(path: str, reintroductions: int = 0) -> PendingFile:
    """O intrare eliberată, cum ar veni ea de la SettleTracker.due()."""
    return PendingFile(
        path=path,
        event_type="file_created",
        occurred_at="2026-01-01T00:00:00+00:00",
        first_seen=100.0,
        last_seen=101.0,
        released_at=102.0,
        reintroduction_count=reintroductions,
    )


@contextlib.contextmanager
def _rewritten_after_the_first_chunk(path: str, replacement: bytes):
    """
    Rescrie fișierul pe disc DUPĂ ce hasher-ul i-a citit primul bloc.

    Fereastra asta nu se poate simula prin os.stat: acolo fișierul s-ar schimba
    înainte de deschidere, iar digest-ul ar descrie totuși o stare completă.
    Aici se schimbă în timpul citirii, deci digest-ul iese rupt — jumătate din
    starea veche, nimic din cea nouă.

    Înlocuirea lui builtins.open e țintită pe o singură cale și restaurată de
    contextul însuși, inclusiv pe excepție. real_open se citește O SINGURĂ dată,
    înainte de înlocuire: un closure captează variabila, nu valoarea, iar o a
    doua atribuire ar face wrapper-ul să se apeleze pe el însuși.
    """
    real_open = builtins.open

    class _MeddlingHandle:
        def __init__(self, handle):
            self._handle = handle
            self._chunks_read = 0

        def read(self, size: int = -1) -> bytes:
            chunk = self._handle.read(size)
            self._chunks_read += 1

            if self._chunks_read == 1:
                # real_open, nu open: aici open e deja înlocuit.
                with real_open(path, "wb") as writer:
                    writer.write(replacement)

            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> bool:
            self._handle.close()
            return False

    def meddling_open(target, *args, **kwargs):
        handle = real_open(target, *args, **kwargs)

        if str(target) == path:
            return _MeddlingHandle(handle)

        return handle

    builtins.open = meddling_open
    try:
        yield
    finally:
        builtins.open = real_open


@contextlib.contextmanager
def _rewritten_after_the_first_stat(path: str, replacement: bytes):
    """
    Rescrie fișierul între primul stat al hasher-ului și deschiderea lui.

    Aceleași precauții ca mai sus: real_stat se citește o dată, iar restaurarea
    aparține contextului.
    """
    real_stat = os.stat
    calls = {"count": 0}

    def meddling_stat(target, *args, **kwargs):
        result = real_stat(target, *args, **kwargs)
        calls["count"] += 1

        if calls["count"] == 1:
            with open(path, "wb") as writer:
                writer.write(replacement)

        return result

    os.stat = meddling_stat
    try:
        yield
    finally:
        os.stat = real_stat


class TempFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)

    def write(self, name: str, content: bytes) -> str:
        path = os.path.join(self._directory.name, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path


class HashSuccessTests(TempFileTestCase):
    def test_a_stable_file_is_hashed_and_reported_as_ok(self) -> None:
        content = b"continut de proba" * 100
        path = self.write("proba.exe", content)

        callback = FlakyCallback()
        _, hasher = _make_hasher(callback)
        hasher.submit(_pending(path))

        self.assertEqual(hasher.process_once(), 1)

        payload = callback.calls[0]
        self.assertEqual(payload["hash_status"], "ok")
        self.assertEqual(payload["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(payload["file_size"], len(content))
        self.assertIn("hash_duration_ms", payload["measurements"])

    def test_an_empty_file_still_produces_a_hash(self) -> None:
        path = self.write("gol.txt", b"")

        callback = FlakyCallback()
        _, hasher = _make_hasher(callback)
        hasher.submit(_pending(path))
        hasher.process_once()

        self.assertEqual(callback.calls[0]["hash_status"], "ok")
        self.assertEqual(callback.calls[0]["file_size"], 0)

    def test_settle_wait_from_the_tracker_survives_into_the_payload(self) -> None:
        path = self.write("proba.exe", b"x")

        callback = FlakyCallback()
        _, hasher = _make_hasher(callback)
        hasher.submit(_pending(path))
        hasher.process_once()

        # PendingFile din _pending(): released_at 102.0 - first_seen 100.0
        self.assertEqual(
            callback.calls[0]["measurements"]["settle_wait_ms"], 2000
        )


class DoubleCheckTests(TempFileTestCase):
    """Inima pasului: un hash al unei stări moarte nu are voie să iasă."""

    def test_a_file_changed_during_the_read_is_reintroduced_not_reported(self) -> None:
        """
        Fereastra pentru care verificarea dublă există: fișierul se schimbă
        ÎNTRE blocurile de citire, nu înainte de ele.

        Digest-ul rezultat acoperă începutul stării vechi și nimic din cea
        nouă — o stare care nu a existat niciodată ca fișier finit. Exact
        hash-ul care nu are voie să ajungă pe fir: serverul l-ar interoga în
        reputație, ar primi „necunoscut" și ar escalada un fișier care poate fi
        perfect cunoscut.
        """
        # Peste _HASH_CHUNK_SIZE (1 MiB), altfel citirea are un singur bloc și
        # nu există „între blocuri".
        original = b"prima stare " * 200_000
        path = self.write("activ.log", original)

        callback = FlakyCallback()
        tracker, hasher = _make_hasher(callback)

        with _rewritten_after_the_first_chunk(path, b"a doua stare, mult mai scurta"):
            hasher.submit(_pending(path))
            self.assertEqual(hasher.process_once(), 0)

        self.assertEqual(
            callback.calls, [], "Un hash al unei stari moarte a ajuns pe fir."
        )
        self.assertEqual(
            tracker.pending_count(),
            1,
            "Fisierul trebuia repus in asteptare, nu abandonat.",
        )

    def test_a_file_changed_before_the_read_is_caught_too(self) -> None:
        """
        Cealaltă jumătate a ferestrei: fișierul se schimbă între primul stat și
        deschidere.

        Aici digest-ul descrie de fapt starea NOUĂ, completă — un hash care ar
        fi fost valid. Îl respingem totuși, deliberat: verificarea dublă compară
        cu ce s-a văzut la primul stat, iar de acolo nu se poate distinge „s-a
        schimbat înainte de citire" de „s-a schimbat în timpul ei". Prudența
        costă o recitire; alternativa ar fi să raportăm un hash despre care nu
        putem demonstra că descrie o stare completă.
        """
        path = self.write("activ.log", b"prima stare")

        callback = FlakyCallback()
        tracker, hasher = _make_hasher(callback)

        with _rewritten_after_the_first_stat(path, b"a doua stare, mai lunga"):
            hasher.submit(_pending(path))
            self.assertEqual(hasher.process_once(), 0)

        self.assertEqual(callback.calls, [])
        self.assertEqual(tracker.pending_count(), 1)

    def test_reintroduction_preserves_the_original_occurred_at(self) -> None:
        tracker = SettleTracker(logger=logging.getLogger("test.tracker"))
        original = _pending("C:/tmp/activ.log")

        tracker.reintroduce(original)
        released = tracker.flush()

        self.assertEqual(len(released), 1)
        self.assertEqual(released[0].occurred_at, original.occurred_at)
        self.assertEqual(released[0].first_seen, original.first_seen)
        self.assertEqual(released[0].reintroduction_count, 1)

    def test_after_the_reintroduction_ceiling_the_file_is_reported_unstable(
        self,
    ) -> None:
        path = self.write("activ.log", b"prima stare")

        callback = FlakyCallback()
        tracker, hasher = _make_hasher(callback, max_reintroductions=2)

        real_stat = os.stat
        state = {"calls": 0}

        def meddling_stat(target, *args, **kwargs):
            result = real_stat(target, *args, **kwargs)
            state["calls"] += 1
            if state["calls"] % 2 == 1:
                with open(path, "ab") as handle:
                    handle.write(b"inca ceva")
            return result

        os.stat = meddling_stat
        try:
            # Intrarea vine deja cu plafonul de reintroduceri atins.
            hasher.submit(_pending(path, reintroductions=2))
            with self.assertLogs("test.hasher", level="INFO"):
                self.assertEqual(hasher.process_once(), 1)
        finally:
            os.stat = real_stat

        payload = callback.calls[0]
        self.assertEqual(payload["hash_status"], "unstable")
        self.assertNotIn("sha256", payload)
        self.assertEqual(tracker.pending_count(), 0)


class FailureStatusTests(TempFileTestCase):
    def test_a_missing_file_is_reported_as_vanished(self) -> None:
        callback = FlakyCallback()
        _, hasher = _make_hasher(callback)
        hasher.submit(_pending(os.path.join(self._directory.name, "nu-exista")))

        hasher.process_once()

        self.assertEqual(callback.calls[0]["hash_status"], "vanished")
        self.assertNotIn("sha256", callback.calls[0])

    def test_an_oversized_file_is_never_read(self) -> None:
        """
        Plafonul de dimensiune trebuie verificat din stat(), inainte de
        deschidere — altfel am plati exact costul pe care vrem sa-l evitam.
        """
        path = self.write("mare.iso", b"x" * 4096)

        callback = FlakyCallback()
        _, hasher = _make_hasher(callback, max_file_size_bytes=1024)

        opened: List[str] = []
        real_open = open

        def tracking_open(target, *args, **kwargs):
            opened.append(str(target))
            return real_open(target, *args, **kwargs)

        import builtins

        builtins.open = tracking_open
        try:
            hasher.submit(_pending(path))
            hasher.process_once()
        finally:
            builtins.open = real_open

        payload = callback.calls[0]
        self.assertEqual(payload["hash_status"], "too_large")
        self.assertEqual(payload["file_size"], 4096)
        self.assertNotIn("sha256", payload)
        self.assertNotIn(path, opened, "Fisierul prea mare a fost totusi deschis.")

    def test_an_unreadable_file_is_reported_as_unreadable(self) -> None:
        path = self.write("blocat.exe", b"date")

        callback = FlakyCallback()
        _, hasher = _make_hasher(callback)

        import builtins

        real_open = builtins.open

        def refusing_open(target, *args, **kwargs):
            if str(target) == path:
                raise PermissionError("simulated lock")
            return real_open(target, *args, **kwargs)

        builtins.open = refusing_open
        try:
            hasher.submit(_pending(path))
            hasher.process_once()
        finally:
            builtins.open = real_open

        self.assertEqual(callback.calls[0]["hash_status"], "unreadable")


class CapacityTests(TempFileTestCase):
    """La presiune se degradează, nu se aruncă — ca la SettleTracker."""

    def test_queue_overflow_degrades_the_arriving_entry(self) -> None:
        callback = FlakyCallback()
        _, hasher = _make_hasher(callback, max_queue_depth=2)

        primul = self.write("primul.exe", b"a")
        al_doilea = self.write("al-doilea.exe", b"b")
        al_treilea = self.write("al-treilea.exe", b"c")

        hasher.submit(_pending(primul))
        hasher.submit(_pending(al_doilea))

        with self.assertLogs("test.hasher", level="WARNING"):
            hasher.submit(_pending(al_treilea))

        self.assertEqual(hasher.queue_depth(), 2)

        hasher.process_once()

        degraded = callback.calls[0]
        self.assertEqual(degraded["hash_status"], "skipped_capacity")
        self.assertIn("al-treilea.exe", degraded["file_path"])
        self.assertNotIn("sha256", degraded)

    def test_the_entry_that_waited_longest_keeps_its_place_in_line(self) -> None:
        """
        Miezul politicii de coadă. _take_next() ia din cap, deci intrarea cea
        mai veche e ȘI cea care a așteptat cel mai mult, ȘI următoarea la rând
        — cea mai aproape de a-și primi hash-ul.

        Degradând-o, aruncăm exact așteptarea pe care tocmai a plătit-o: a stat
        la rând tot drumul până în față ca să fie refuzată la un pas de citire.
        """
        callback = FlakyCallback()
        _, hasher = _make_hasher(callback, max_queue_depth=2)

        hasher.submit(_pending(self.write("asteapta-de-mult.exe", b"a")))
        hasher.submit(_pending(self.write("al-doilea.exe", b"b")))

        for index in range(3):
            with self.assertLogs("test.hasher", level="WARNING"):
                hasher.submit(_pending(self.write(f"navala-{index}.exe", b"x")))

        hasher.process_once()

        hashed = [call for call in callback.calls if call["hash_status"] == "ok"]
        self.assertEqual(len(hashed), 1)
        self.assertIn(
            "asteapta-de-mult.exe",
            hashed[0]["file_path"],
            "Fisierul care astepta de cel mai mult timp a fost degradat, iar o "
            "sosire proaspata i-a luat locul la hash.",
        )

    def test_a_degraded_entry_is_reported_not_dropped(self) -> None:
        callback = FlakyCallback()
        _, hasher = _make_hasher(callback, max_queue_depth=1)

        # Primul submit incape; urmatoarele trei nu mai au loc si sunt
        # degradate chiar la sosire.
        hasher.submit(_pending(self.write("f0.exe", b"x")))

        for index in range(1, 4):
            path = self.write(f"f{index}.exe", b"x")
            with self.assertLogs("test.hasher", level="WARNING"):
                hasher.submit(_pending(path))

        # Trei degradate + una hash-uita normal. Numarul nu depinde de politica
        # de coada — el e dat de viteza hasher-ului. Ce depinde de politica e
        # CARE fisier primeste hash-ul.
        total = 0
        for _ in range(3):
            total += hasher.process_once()

        self.assertEqual(total, 4)
        statuses = [call["hash_status"] for call in callback.calls]
        self.assertEqual(statuses.count("skipped_capacity"), 3)
        self.assertEqual(statuses.count("ok"), 1)

        hashed = next(
            call for call in callback.calls if call["hash_status"] == "ok"
        )
        self.assertIn("f0.exe", hashed["file_path"])

    def test_pressure_never_turns_into_silence(self) -> None:
        """
        Garda care nu are voie să cadă, indiferent de capătul ales: fiecare
        fișier predat hasher-ului produce exact un eveniment. Presiunea se
        convertește în raportare degradată, niciodată în tăcere.
        """
        callback = FlakyCallback()
        _, hasher = _make_hasher(callback, max_queue_depth=2)

        submitted = 12

        # Avertismentele de capacitate sunt asteptate aici si verificate in
        # alta parte; le taiem ca sa nu umple iesirea suitei.
        logging.getLogger("test.hasher").setLevel(logging.CRITICAL)
        self.addCleanup(
            logging.getLogger("test.hasher").setLevel, logging.NOTSET
        )

        for index in range(submitted):
            hasher.submit(_pending(self.write(f"f{index}.exe", b"x")))

        while hasher.process_once():
            pass

        self.assertEqual(
            len(callback.calls),
            submitted,
            "Un fisier predat hasher-ului nu a produs niciun eveniment.",
        )


class EmitRetryTests(TempFileTestCase):
    def test_a_failed_delivery_is_retried_with_a_stable_client_event_id(self) -> None:
        path = self.write("proba.exe", b"date")

        callback = FlakyCallback(fail_times=1)
        _, hasher = _make_hasher(callback)
        hasher.submit(_pending(path))

        with self.assertLogs("test.hasher", level="WARNING"):
            self.assertEqual(hasher.process_once(), 0)

        self.assertEqual(hasher.process_once(), 1)
        self.assertEqual(len(callback.calls), 2)
        self.assertEqual(
            callback.calls[0]["client_event_id"],
            callback.calls[1]["client_event_id"],
        )

    def test_after_the_attempt_ceiling_the_event_is_dropped_with_an_error(
        self,
    ) -> None:
        path = self.write("proba.exe", b"date")

        callback = FlakyCallback(fail_times=100)
        _, hasher = _make_hasher(callback, max_emit_attempts=3)
        hasher.submit(_pending(path))

        with self.assertLogs("test.hasher", level="WARNING"):
            hasher.process_once()
        with self.assertLogs("test.hasher", level="WARNING"):
            hasher.process_once()
        with self.assertLogs("test.hasher", level="ERROR"):
            hasher.process_once()

        self.assertEqual(hasher.process_once(), 0)
        self.assertEqual(len(callback.calls), 3)


class ShutdownDrainTests(TempFileTestCase):
    def test_drain_hashes_what_fits_in_the_budget(self) -> None:
        callback = FlakyCallback()
        clock = FakeClock()
        _, hasher = _make_hasher(callback, clock=clock, shutdown_budget_seconds=10.0)

        for index in range(3):
            hasher.submit(_pending(self.write(f"f{index}.exe", b"x")))

        self.assertEqual(hasher.drain_for_shutdown(), 3)
        self.assertTrue(
            all(call["hash_status"] == "ok" for call in callback.calls)
        )

    def test_what_the_budget_does_not_cover_is_reported_without_a_hash(self) -> None:
        """
        Termenul mărginește HASHING-ul, nu RAPORTAREA — și aici se vede că sunt
        două lucruri diferite.

        Un buget de zero secunde produce zero hash-uri și DOUĂ evenimente. Nu e
        o scăpare: raportarea e ieftină și obligatorie, hashing-ul e scump și
        opțional. A plafona raportarea ar transforma presiunea de timp în
        tăcere, adică exact ce mecanismul interzice peste tot.

        (Prima versiune a testului afirma același număr, dar sub numele de
        „buget expirat" — ceea ce citit atent spunea că munca se face în ciuda
        bugetului, nu că bugetul acoperă altceva.)
        """
        callback = FlakyCallback()
        _, hasher = _make_hasher(callback, shutdown_budget_seconds=0.0)

        for index in range(2):
            hasher.submit(_pending(self.write(f"f{index}.exe", b"x")))

        with self.assertLogs("test.hasher", level="WARNING"):
            self.assertEqual(hasher.drain_for_shutdown(), 2)

        statuses = [call["hash_status"] for call in callback.calls]
        self.assertEqual(statuses, ["skipped_shutdown", "skipped_shutdown"])

    def test_a_read_already_in_progress_is_abandoned_at_the_deadline(self) -> None:
        """
        Miezul reparației. Verificat doar înainte de fișier, termenul mărginea
        momentul ultimei PORNIRI, nu durata muncii: un fișier de 200 MB pornit
        cu o milisecundă înainte de expirare se citea integral.

        Aici ceasul avansează la fiecare citire de bloc, deci termenul cade în
        mijlocul citirii. Fișierul trebuie raportat 'skipped_shutdown', fără
        sha256 — dar CU file_size, care vine din stat() și e cunoscut chiar
        dacă citirea n-a fost dusă până la capăt.
        """
        # Peste _HASH_CHUNK_SIZE (1 MiB), ca să existe mai multe treceri.
        path = self.write("mare.iso", b"x" * (3 * 1024 * 1024))

        callback = FlakyCallback()
        clock = TickingClock(step=1.0)
        _, hasher = _make_hasher(callback, clock=clock)

        hasher.submit(_pending(path))
        hasher.stop(deadline=clock.now + 3.0)

        with self.assertLogs("test.hasher", level="WARNING"):
            hasher.drain_for_shutdown()

        payload = callback.calls[0]
        self.assertEqual(payload["hash_status"], "skipped_shutdown")
        self.assertNotIn("sha256", payload)
        self.assertEqual(payload["file_size"], 3 * 1024 * 1024)

    def test_the_same_file_hashes_fine_without_a_deadline(self) -> None:
        """
        Martorul testului de mai sus: același fișier, același ceas care
        avansează, dar fără termen. Fără el, abandonul nu s-ar putea atribui
        termenului.
        """
        content = b"x" * (3 * 1024 * 1024)
        path = self.write("mare.iso", content)

        callback = FlakyCallback()
        _, hasher = _make_hasher(callback, clock=TickingClock(step=1.0))

        hasher.submit(_pending(path))
        self.assertEqual(hasher.process_once(), 1)

        payload = callback.calls[0]
        self.assertEqual(payload["hash_status"], "ok")
        self.assertEqual(payload["sha256"], hashlib.sha256(content).hexdigest())

    def test_the_deadline_comes_from_the_caller_not_from_drain_entry(self) -> None:
        """
        Termenul e un MOMENT dat de apelant, nu o durată numărată de când
        firul a apucat să înceapă drenarea.

        Distincția contează pentru că întârzierea până la intrarea în drenare
        e nemărginită: semnalul de oprire nu se observă cât timp firul e blocat
        într-o citire. Un buget măsurat de acolo încolo n-ar avea nicio legătură
        cu momentul în care apelantul a cerut oprirea.
        """
        callback = FlakyCallback()
        clock = FakeClock()
        _, hasher = _make_hasher(
            callback, clock=clock, shutdown_budget_seconds=100.0
        )

        hasher.submit(_pending(self.write("proba.exe", b"x")))

        # Apelantul a cerut oprirea cu termen în trecut: bugetul implicit de
        # 100s nu are voie să-l suprascrie.
        hasher.stop(deadline=clock.now - 1.0)

        with self.assertLogs("test.hasher", level="WARNING"):
            hasher.drain_for_shutdown()

        self.assertEqual(callback.calls[0]["hash_status"], "skipped_shutdown")

    def test_drain_gives_each_failure_a_single_attempt(self) -> None:
        callback = FlakyCallback(fail_times=100)
        _, hasher = _make_hasher(callback, shutdown_budget_seconds=10.0)
        hasher.submit(_pending(self.write("proba.exe", b"x")))

        with self.assertLogs("test.hasher", level="ERROR"):
            self.assertEqual(hasher.drain_for_shutdown(), 0)

        self.assertEqual(len(callback.calls), 1)


class WorkerThreadTests(TempFileTestCase):
    def test_the_thread_hashes_end_to_end_and_stops_cleanly(self) -> None:
        captured: List[dict] = []
        arrived = threading.Event()

        def callback(payload: dict) -> None:
            captured.append(payload)
            arrived.set()

        content = b"date de proba"
        path = self.write("proba.exe", content)

        _, hasher = _make_hasher(callback, poll_seconds=0.01)
        hasher.start()
        try:
            hasher.submit(_pending(path))
            self.assertTrue(
                arrived.wait(timeout=5.0),
                "Firul hasher-ului nu a emis evenimentul in timp util.",
            )
        finally:
            hasher.stop()
            hasher.join(timeout=5.0)

        self.assertEqual(captured[0]["sha256"], hashlib.sha256(content).hexdigest())

    def test_the_loop_survives_an_unexpected_exception(self) -> None:
        _, hasher = _make_hasher(lambda payload: None, poll_seconds=0.01)
        calls = {"count": 0}

        def exploding_pass() -> int:
            calls["count"] += 1
            raise RuntimeError("simulated pass failure")

        hasher.process_once = exploding_pass  # type: ignore[method-assign]

        with self.assertLogs("test.hasher", level="ERROR"):
            hasher.start()
            try:
                deadline = time.monotonic() + 5.0
                while calls["count"] < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                hasher.stop()
                hasher.join(timeout=5.0)

        self.assertGreaterEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()