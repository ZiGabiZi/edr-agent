"""
Criteriul Etapei 0, demonstrat prin lanțul real.
================================================

Un fișier scris în bucăți produce exact un eveniment, cu hash-ul conținutului
final.

De ce exista un gol aici:
    Fiecare etaj al fluxului era testat izolat — tracker-ul singur, hasher-ul
    singur, oprirea singură — iar `Observer`-ul watchdog nu era instanțiat de
    niciun test. Lanțul complet, de la sistemul de fișiere până în coada
    persistentă, nu fusese exercitat niciodată ca un tot.

    Diferența nu e academică. Toate testele de până acum presupun că watchdog
    se comportă cum credem noi: câte evenimente emite pentru o scriere, în ce
    ordine, cu ce căi. Testele cu dubluri nu pot infirma presupunerea, pentru
    că ele *sunt* presupunerea. Aici o punem față în față cu sistemul real.

Ce e diferit la testele de aici, și de ce merită știut înainte de a le atinge:
    Sunt singurele din suită care depind de ceasul real și de sistemul de
    fișiere al mașinii. Regula pe care o respectă toate: se așteaptă pe o
    CONDIȚIE, cu termen-limită generos, niciodată pe o durată fixă. Un
    `sleep(0.5)` care merge pe o mașină liberă devine intermitent pe una
    încărcată, iar un test care pică aleatoriu e mai rău decât niciun test:
    îl învață pe om să ignore culoarea roșie.

    Din același motiv perioada de liniște e mică (0.4s) dar termenele sunt
    mari (10s). Nu ne interesează *cât de repede* se întâmplă, ci *dacă* se
    întâmplă — viteza e reglaj, corectitudinea e contract.
"""

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List

import agent
from services.event_spool import EventSpool


_SETTLE_QUIET_SECONDS = 0.4
_WAIT_TIMEOUT_SECONDS = 10.0
_POLL_SECONDS = 0.02


class _RecordingDispatcher:
    """
    Dublura minimă a dispatcher-ului: doar wake(), exact cât cere callback-ul.

    NU e pornit niciun fir. Testele verifică ce ajunge în coadă, nu livrarea în
    rețea — aceea are testele ei, cu transportul înlocuit.
    """

    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


def _wait_until(predicate: Callable[[], bool], timeout: float = _WAIT_TIMEOUT_SECONDS) -> bool:
    """Așteaptă o condiție, nu o durată. Vezi nota din antetul modulului."""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_SECONDS)

    return predicate()


class FilePipelineEndToEndTests(unittest.TestCase):
    """Lanțul complet: watchdog -> tracker -> hasher -> spool."""

    def setUp(self) -> None:
        watched = tempfile.TemporaryDirectory(prefix="edr-e2e-watched-")
        state = tempfile.TemporaryDirectory(prefix="edr-e2e-state-")
        self.addCleanup(watched.cleanup)
        self.addCleanup(state.cleanup)

        self.watched_dir = Path(watched.name)
        self.spool = EventSpool(db_path=Path(state.name) / "event_spool.db")
        self.addCleanup(self.spool.close)

        self.dispatcher = _RecordingDispatcher()
        self.monitor = None

    def _start_monitor(self, **tuning: Any) -> None:
        config: Dict[str, Any] = {
            "agent_id": "endpoint-e2e",
            "agent_instance_id": "inst-e2e",
            "monitored_directories": [str(self.watched_dir)],
            "recursive_monitoring": True,
            # Perioadă de liniște mică, ca testul să dureze secunde, nu minute.
            # Cadența firului de eliberare trebuie să rămână mult sub ea, altfel
            # ar adăuga singură o latență comparabilă cu ce măsurăm.
            "settle_quiet_seconds": _SETTLE_QUIET_SECONDS,
            "release_poll_seconds": 0.05,
            "settle_max_wait_seconds": 10.0,
            "shutdown_hash_budget_seconds": 5.0,
            "shutdown_report_reserve_seconds": 1.0,
        }
        config.update(tuning)

        self.monitor = agent.start_file_monitoring(
            config,
            agent.build_file_event_callback(self.spool, self.dispatcher),  # type: ignore[arg-type]
        )
        self.assertIsNotNone(self.monitor, "monitorul trebuie să pornească")
        self.addCleanup(self._stop_monitor)

    def _stop_monitor(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor.join(timeout=5)
            self.monitor = None

    # -- ajutoare -----------------------------------------------------------

    def _write_in_chunks(self, name: str, chunks: List[bytes], pause: float) -> bytes:
        """
        Scrie un fișier în bucăți, deschizând și închizând la fiecare bucată.

        Aşa se comportă multe programe reale (append-and-close), iar pe Windows
        evită întrebarea separată despre partajarea unui handle deschis pentru
        scriere — care ar fi un al doilea subiect, nu cel testat aici.
        """
        path = self.watched_dir / name

        for chunk in chunks:
            with path.open("ab") as handle:
                handle.write(chunk)
                handle.flush()
            time.sleep(pause)

        return b"".join(chunks)

    def _spooled_events(self) -> List[Dict[str, Any]]:
        return [payload for _, payload in self.spool.peek_batch(100)]

    def _wait_for_events(self, count: int) -> List[Dict[str, Any]]:
        _wait_until(lambda: len(self._spooled_events()) >= count)
        return self._spooled_events()

    # -- criteriul ----------------------------------------------------------

    def test_a_file_written_in_chunks_produces_exactly_one_event(self) -> None:
        """
        Criteriul Etapei 0, literal.

        Fiecare bucată scrisă produce cel puțin un eveniment de sistem de
        fișiere. Dacă mecanismul de liniștire n-ar coalesce, aici ar apărea trei
        sau mai multe evenimente pentru un singur fișier — iar consola ar arăta
        activitate inventată de instrumentul de observare, nu de mașină.
        """
        self._start_monitor()

        content = self._write_in_chunks(
            "raport.bin", [b"prima-", b"a-doua-", b"a-treia"], pause=0.1
        )

        events = self._wait_for_events(1)

        self.assertEqual(
            len(events),
            1,
            f"un fișier scris în bucăți trebuie să producă exact un eveniment, "
            f"nu {len(events)}: {[e.get('file_path') for e in events]}",
        )

        event = events[0]
        self.assertEqual(event["hash_status"], "ok")
        self.assertEqual(event["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(event["file_size"], len(content))

    def test_the_hash_is_of_the_final_content_not_an_intermediate_state(self) -> None:
        """
        Partea a doua a criteriului, formulată ca infirmare.

        Un hash calculat prea devreme e mai rău decât niciun hash: e SHA-256-ul
        unei stări care n-a existat niciodată ca fișier finit. Serverul l-ar
        interoga într-o reputație, ar primi „necunoscut" și ar escalada. Cazul
        rău e zgomotul; cazul foarte rău e un fișier malițios cunoscut care
        trece drept necunoscut fiindcă amprenta lui nu se potrivește cu nimic.
        """
        self._start_monitor()

        first_chunk = b"inceput-care-nu-trebuie-hash-uit"
        content = self._write_in_chunks(
            "in-crestere.bin", [first_chunk, b"-si-restul"], pause=0.15
        )

        event = self._wait_for_events(1)[0]

        self.assertEqual(event["sha256"], hashlib.sha256(content).hexdigest())
        self.assertNotEqual(
            event["sha256"],
            hashlib.sha256(first_chunk).hexdigest(),
            "hash-ul aparține unei stări intermediare, nu fișierului finit",
        )

    def test_the_coalescing_is_per_file_not_global(self) -> None:
        """
        Două fișiere scrise în paralel rămân două evenimente.

        Fără garda asta, o implementare care ar coalesce pe fereastră de timp în
        loc de pe fișier ar trece testul de mai sus și ar pierde tăcut tot ce se
        scrie simultan — adică exact tiparul unei rafale reale.
        """
        self._start_monitor()

        first = self.watched_dir / "unu.bin"
        second = self.watched_dir / "doi.bin"
        first.write_bytes(b"continutul-unu")
        second.write_bytes(b"continutul-doi")

        events = self._wait_for_events(2)
        paths = sorted(Path(event["file_path"]).name for event in events)

        self.assertEqual(paths, ["doi.bin", "unu.bin"])
        self.assertEqual(
            [event["sha256"] for event in events].count(None), 0, "ambele trebuie hash-uite"
        )

    def test_the_event_reaches_the_persistent_spool_and_wakes_the_dispatcher(self) -> None:
        """
        Ultimele două verigi: coada e cea persistentă, iar producătorul chiar
        anunță consumatorul. Fără wake(), evenimentul ar pleca oricum la
        următorul ciclu de polling, dar cu o latență pe care nimeni nu a ales-o.
        """
        self._start_monitor()
        (self.watched_dir / "trezeste.bin").write_bytes(b"continut")

        self._wait_for_events(1)

        self.assertEqual(self.spool.pending_count(), 1)
        self.assertGreaterEqual(self.dispatcher.wakes, 1)

    def test_a_file_still_settling_at_shutdown_is_drained_not_lost(self) -> None:
        """
        Drenarea la oprire, pe lanțul real.

        Un fișier care încă se liniștește când vine semnalul de oprire ar fi
        pierdut definitiv fără drenare: firele de hashing și de eliberare sunt
        daemon, deci ar muri odată cu procesul, în mijlocul a ce fac. Bugetul de
        oprire există exact pentru fereastra asta.

        Perioada de liniște e ridicată deliberat la cinci secunde. Cu ea, fișierul
        NU are cum să fie eliberat pe calea normală în timpul testului, deci orice
        eveniment ajuns în coadă provine cu certitudine din drenare. Cu valoarea
        implicită mică, testul ar fi trecut și dacă drenarea n-ar fi existat.

        Se așteaptă întâi ca tracker-ul să fi înregistrat fișierul. Fără pasul
        ăsta, testul ar cere o garanție pe care sistemul nu o are: un fișier pe
        care observer-ul nu a apucat să îl vadă înainte de oprire nu e „pierdut",
        e necunoscut. Granița contează — prima versiune a testului o încălca și
        pica intermitent, arătând ca un defect care nu exista.
        """
        self._start_monitor(settle_quiet_seconds=5.0, settle_max_wait_seconds=30.0)

        content = b"scris-in-ultima-clipa"
        (self.watched_dir / "la-limita.bin").write_bytes(content)

        observed = _wait_until(
            lambda: self.monitor.tracker.pending_count() >= 1  # type: ignore[union-attr]
        )
        self.assertTrue(observed, "observer-ul nu a înregistrat fișierul în timp util")
        self.assertEqual(
            self._spooled_events(), [], "fișierul nu are voie să fi fost deja eliberat"
        )

        self.monitor.stop()  # type: ignore[union-attr]
        stopped = self.monitor.join(timeout=10)  # type: ignore[union-attr]
        self.monitor = None

        self.assertTrue(stopped, "toate etajele trebuie să se oprească în buget")

        events = self._spooled_events()
        self.assertEqual(len(events), 1, "drenarea trebuie să emită fișierul în așteptare")
        self.assertEqual(events[0]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(events[0]["hash_status"], "ok")


if __name__ == "__main__":
    unittest.main()
