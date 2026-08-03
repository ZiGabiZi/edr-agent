"""
Teste pentru EventDebouncer și pentru garda de curățare a intrărilor vechi.

De ce există acest fișier:
    Debouncer-ul ține un dicționar cu ultima apariție a fiecărei perechi
    (tip eveniment, cale). Dicționarul crește cu fiecare fișier distinct atins
    pe endpoint, deci trebuie curățat periodic. Garda care decide *când* se
    curăță are nouă linii și a purtat deja două bug-uri opuse, niciunul vizibil
    din afara clasei: `is_duplicate` returnează exact aceleași valori în toate
    cele trei variante. Diferă doar cât memorie se reține și cât timp se petrece
    sub lock, pe firul observatorului watchdog.

    bcf300c — două return-uri separate (sub 500 de chei -> return; sub 60 de
    secunde -> return) însemnau că amândouă condițiile trebuiau îndeplinite ca
    să se curețe ceva. Pe orice volum care nu ajungea la 500 de chei distincte,
    `_cleanup_stale_entries` nu rula niciodată, iar intrările vechi rămâneau
    până la oprirea procesului.

    e08c798 — rescris ca `if not (prag_depășit or timp_scurs): return`. A
    reparat cazul de mai sus, dar odată ce dicționarul trecea de 500 de intrări,
    garda lăsa să treacă *fiecare* apel: o reconstrucție O(n) a dicționarului
    per eveniment, sub lock, pe firul observatorului.

    87af315 — a rămas doar limita de timp. Aceasta e varianta curentă.

    Testele de mai jos fixează contractul (ce se șterge și ce se păstrează) și,
    separat, ambele regresii istorice: una verifică faptul că se curăță și
    atunci când dicționarul e mic, cealaltă că se curăță cel mult o dată la
    interval, oricât de multe evenimente ar veni. Pentru că bug-urile nu se văd
    prin API-ul public, aceste două teste inspectează direct starea internă
    (`_last_seen`, `_last_cleanup_time`) — este singurul punct de observare.

    Ultima clasă acoperă plafonul de capacitate, mecanismul separat adăugat
    pentru problema pe care garda de timp nu o poate rezolva: o rafală mai
    scurtă decât intervalul de curățare nu este atinsă niciodată de gardă, deci
    vârful de memorie rămânea „un minut întreg de chei distincte" — aceeași
    expunere ca varianta AND originală. Cele două mecanisme sunt testate
    izolat: testele gărzii de timp fixează explicit un plafon peste volumul
    folosit, ca evicțiunea să nu poată masca o gardă defectă.
"""

import os
import unittest

from services.file_monitor import (
    _DEBOUNCE_CLEANUP_INTERVAL_SECONDS,
    _DEBOUNCE_MAX_TRACKED_EVENTS,
    EventDebouncer,
    normalize_file_path,
)


class FakeClock:
    """
    Ceas controlat de test, cu aceeași semantică ca time.monotonic().

    Pornește deliberat de la o valoare mare, nu de la zero: time.monotonic() nu
    întoarce niciodată ~0 pe un sistem care rulează, iar `_last_cleanup_time`
    pornește de la 0.0. Prima verificare a gărzii vede deci mereu o distanță
    uriașă și declanșează o curățare — comportament real, pe care testele îl
    iau în calcul explicit.
    """

    def __init__(self, start: float = 10_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _event_key(event_type: str, file_path: str) -> str:
    """Reconstruiește cheia folosită intern de EventDebouncer."""
    return f"{event_type}:{os.path.normcase(normalize_file_path(file_path))}"


class CleanupGuardTests(unittest.TestCase):
    """Garda decide cât de des rulează curățarea, nu dacă rulează vreodată."""

    def test_cleanup_runs_even_when_the_dictionary_stays_small(self) -> None:
        """
        300 de chei distincte în 10 minute: dicționarul nu trece niciodată de
        500 de intrări, dar timpul dintre curățări e depășit de multe ori.

        Cu varianta AND (bcf300c) curățarea nu rulează nici măcar o dată și
        toate cele 300 de chei rămân în memorie.
        """
        total_keys = 300

        clock = FakeClock()
        # Plafon peste volumul testului: singurul mecanism care poate reduce
        # dicționarul aici este garda de timp, nu evicțiunea.
        debouncer = EventDebouncer(
            interval_seconds=2.0, clock=clock, max_tracked_events=total_keys
        )

        step_seconds = 600.0 / total_keys

        for index in range(total_keys):
            debouncer.is_duplicate("file_modified", f"C:/tmp/file_{index}.txt")
            clock.advance(step_seconds)

        self.assertLess(
            len(debouncer._last_seen),
            total_keys,
            "Nicio intrare nu a fost eliminată în 10 minute: garda cere și "
            "depășirea unui prag de dimensiune, nu doar trecerea timpului.",
        )

    def test_cleanup_body_is_rate_limited_regardless_of_event_volume(self) -> None:
        """
        20.000 de evenimente într-o rafală de 20 de secunde: sub limita de timp
        încape o singură curățare (cea declanșată de primul apel).

        Cu varianta OR (e08c798) corpul rulează de 19.500 ori: trecerea
        inițială, plus câte una pentru fiecare eveniment de după a 501-a cheie
        — fiecare rulare reconstruind un dicționar de zeci de mii de intrări,
        sub lock.
        """
        total_events = 20_000
        burst_seconds = 20.0

        clock = FakeClock()
        # Ca mai sus: plafonul este scos din ecuație, se măsoară doar garda.
        debouncer = EventDebouncer(
            interval_seconds=2.0, clock=clock, max_tracked_events=total_events
        )

        step_seconds = burst_seconds / total_events

        cleanup_runs = 0
        for index in range(total_events):
            now = clock.now
            debouncer.is_duplicate("file_modified", f"C:/tmp/file_{index}.txt")
            # Corpul curățării este singurul care scrie `_last_cleanup_time`,
            # deci egalitatea cu momentul apelului curent înseamnă că a rulat.
            if debouncer._last_cleanup_time == now:
                cleanup_runs += 1
            clock.advance(step_seconds)

        max_expected_runs = 1 + int(
            burst_seconds // _DEBOUNCE_CLEANUP_INTERVAL_SECONDS
        )

        self.assertLessEqual(
            cleanup_runs,
            max_expected_runs,
            f"Curățarea a rulat de {cleanup_runs} ori pentru {total_events} "
            f"evenimente strânse în {burst_seconds:g} secunde.",
        )

    def test_pass_drops_entries_older_than_two_intervals_and_keeps_newer(self) -> None:
        """Contractul propriu-zis al curățării, verificat pe o singură trecere."""
        clock = FakeClock()
        interval_seconds = 2.0
        debouncer = EventDebouncer(
            interval_seconds=interval_seconds, clock=clock
        )

        old_path = "C:/tmp/vechi.txt"
        recent_path = "C:/tmp/recent.txt"
        trigger_path = "C:/tmp/declanșator.txt"

        # Primul apel consumă curățarea inițială (`_last_cleanup_time` == 0.0).
        debouncer.is_duplicate("file_modified", old_path)

        clock.advance(_DEBOUNCE_CLEANUP_INTERVAL_SECONDS - interval_seconds)
        debouncer.is_duplicate("file_modified", recent_path)

        # Apelul următor cade exact pe limita de timp, deci curăță: intrarea
        # veche are 60 s, cea recentă are 2 s, iar pragul e 2 * interval = 4 s.
        clock.advance(interval_seconds)
        debouncer.is_duplicate("file_modified", trigger_path)

        remaining_keys = set(debouncer._last_seen)

        self.assertNotIn(
            _event_key("file_modified", old_path),
            remaining_keys,
            "Intrarea mai veche decât 2 * interval trebuia eliminată.",
        )
        self.assertIn(
            _event_key("file_modified", recent_path),
            remaining_keys,
            "Intrarea mai nouă decât 2 * interval trebuia păstrată.",
        )


class IsDuplicateTests(unittest.TestCase):
    """Semantica de deduplicare văzută de handler-ul de evenimente."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.interval_seconds = 2.0
        self.debouncer = EventDebouncer(
            interval_seconds=self.interval_seconds, clock=self.clock
        )

    def test_first_sighting_is_not_a_duplicate(self) -> None:
        self.assertFalse(
            self.debouncer.is_duplicate("file_modified", "C:/tmp/a.txt")
        )

    def test_second_sighting_within_the_interval_is_a_duplicate(self) -> None:
        self.debouncer.is_duplicate("file_modified", "C:/tmp/a.txt")

        self.clock.advance(self.interval_seconds / 2)

        self.assertTrue(
            self.debouncer.is_duplicate("file_modified", "C:/tmp/a.txt")
        )

    def test_sighting_after_the_interval_elapsed_is_not_a_duplicate(self) -> None:
        self.debouncer.is_duplicate("file_modified", "C:/tmp/a.txt")

        self.clock.advance(self.interval_seconds)

        self.assertFalse(
            self.debouncer.is_duplicate("file_modified", "C:/tmp/a.txt")
        )

    def test_different_paths_and_event_types_are_tracked_separately(self) -> None:
        self.debouncer.is_duplicate("file_modified", "C:/tmp/a.txt")

        self.assertFalse(
            self.debouncer.is_duplicate("file_modified", "C:/tmp/b.txt")
        )
        self.assertFalse(
            self.debouncer.is_duplicate("file_created", "C:/tmp/a.txt")
        )

    def test_default_clock_is_the_real_monotonic_clock(self) -> None:
        """Injectarea ceasului nu trebuie să schimbe comportamentul implicit."""
        debouncer = EventDebouncer(interval_seconds=60.0)

        self.assertFalse(debouncer.is_duplicate("file_modified", "C:/tmp/a.txt"))
        self.assertTrue(debouncer.is_duplicate("file_modified", "C:/tmp/a.txt"))


class CapacityEvictionTests(unittest.TestCase):
    """Plafonul mărginește memoria acolo unde garda de timp nu ajunge."""

    def _make(self, max_tracked_events: int, interval_seconds: float = 2.0):
        clock = FakeClock()
        debouncer = EventDebouncer(
            interval_seconds=interval_seconds,
            clock=clock,
            max_tracked_events=max_tracked_events,
        )
        return clock, debouncer

    def test_dictionary_never_exceeds_the_cap_during_a_burst(self) -> None:
        """
        Aceeași rafală de 20.000 de evenimente în 20 de secunde: prea scurtă ca
        garda de timp să intervină mai mult de o dată. Fără plafon, vârful este
        de 20.000 de intrări; cu plafon, este exact plafonul.
        """
        cap = 500
        clock, debouncer = self._make(cap)

        total_events = 20_000
        step_seconds = 20.0 / total_events

        peak_entries = 0
        for index in range(total_events):
            debouncer.is_duplicate("file_modified", f"C:/tmp/file_{index}.txt")
            peak_entries = max(peak_entries, len(debouncer._last_seen))
            clock.advance(step_seconds)

        self.assertLessEqual(
            peak_entries,
            cap,
            f"Vârful de {peak_entries} intrări a depășit plafonul de {cap}.",
        )

    def test_eviction_drops_the_least_recently_seen_entry(self) -> None:
        clock, debouncer = self._make(3)

        for name in ("a", "b", "c"):
            debouncer.is_duplicate("file_modified", f"C:/tmp/{name}.txt")
            clock.advance(0.1)

        debouncer.is_duplicate("file_modified", "C:/tmp/d.txt")

        remaining_keys = set(debouncer._last_seen)

        self.assertEqual(len(debouncer._last_seen), 3)
        self.assertNotIn(_event_key("file_modified", "C:/tmp/a.txt"), remaining_keys)
        for name in ("b", "c", "d"):
            self.assertIn(
                _event_key("file_modified", f"C:/tmp/{name}.txt"), remaining_keys
            )

    def test_reseeing_a_key_protects_it_from_eviction(self) -> None:
        """
        Ordinea trebuie să fie a *ultimei* apariții, nu a primei: un fișier
        atins constant nu are voie să fie evacuat înaintea unuia inactiv.
        """
        clock, debouncer = self._make(3)

        for name in ("a", "b", "c"):
            debouncer.is_duplicate("file_modified", f"C:/tmp/{name}.txt")
            clock.advance(0.1)

        debouncer.is_duplicate("file_modified", "C:/tmp/a.txt")
        clock.advance(0.1)

        debouncer.is_duplicate("file_modified", "C:/tmp/d.txt")

        remaining_keys = set(debouncer._last_seen)

        self.assertIn(
            _event_key("file_modified", "C:/tmp/a.txt"),
            remaining_keys,
            "Cheia revăzută a fost evacuată în locul celei mai vechi.",
        )
        self.assertNotIn(
            _event_key("file_modified", "C:/tmp/b.txt"), remaining_keys
        )

    def test_ordering_survives_a_cleanup_pass(self) -> None:
        """
        Curățarea periodică reconstruiește dicționarul. Dacă reconstrucția ar
        pierde ordinea, evicțiunea de după ar elimina intrarea greșită.
        """
        # Interval mare: pragul curățării (2 * interval) nu elimină nimic aici,
        # deci testul măsoară strict ordinea, nu vechimea.
        clock, debouncer = self._make(3, interval_seconds=100.0)

        for name in ("a", "b", "c"):
            debouncer.is_duplicate("file_modified", f"C:/tmp/{name}.txt")
            clock.advance(1.0)

        # Trece limita de timp: acest apel declanșează curățarea și, imediat
        # după ea, mută cheia "a" la capătul cel mai recent.
        clock.advance(_DEBOUNCE_CLEANUP_INTERVAL_SECONDS)
        debouncer.is_duplicate("file_modified", "C:/tmp/a.txt")
        clock.advance(1.0)

        debouncer.is_duplicate("file_modified", "C:/tmp/d.txt")

        remaining_keys = set(debouncer._last_seen)

        self.assertIn(_event_key("file_modified", "C:/tmp/a.txt"), remaining_keys)
        self.assertNotIn(
            _event_key("file_modified", "C:/tmp/b.txt"), remaining_keys
        )

    def test_a_key_within_the_cap_still_deduplicates(self) -> None:
        """Plafonul nu are voie să strice deduplicarea obișnuită."""
        clock, debouncer = self._make(3)

        debouncer.is_duplicate("file_modified", "C:/tmp/a.txt")
        for name in ("b", "c"):
            clock.advance(0.1)
            debouncer.is_duplicate("file_modified", f"C:/tmp/{name}.txt")

        clock.advance(0.1)

        self.assertTrue(
            debouncer.is_duplicate("file_modified", "C:/tmp/a.txt")
        )

    def test_default_cap_is_the_module_constant(self) -> None:
        self.assertEqual(
            EventDebouncer().max_tracked_events, _DEBOUNCE_MAX_TRACKED_EVENTS
        )


if __name__ == "__main__":
    unittest.main()
