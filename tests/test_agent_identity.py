"""
Identitatea mașinii: derivare, normalizare, înghețare.
======================================================

Ordinea testelor urmează ordinea în care lucrurile pot să tacă:

  1. DERIVAREA — forma și determinismul. O derivare nedeterministă ar produce
     o înregistrare nouă pe server la fiecare reinstalare.
  2. NORMALIZAREA — valoarea ajunge într-o cale de URL și în consolă.
  3. ÎNGHEȚAREA — partea fără de care nimic din primele două nu contează:
     un agent_id recalculat la fiecare pornire se poate schimba, iar
     schimbarea lui înseamnă 403 permanent, pentru că vechea cheie de API e
     emisă pentru vechiul nume.
  4. PRECEDENȚA — config bate fișierul, fișierul bate derivarea.
"""

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from services.agent_credentials import DEFAULT_AGENT_KEY_PATH
from services.agent_identity import (
    DEFAULT_AGENT_ID_PATH,
    derive_agent_id,
    normalize_id_part,
    resolve_agent_id,
)


_HASH = "9a3650f714402951f368e37c5b6dccc1dd84fe11fb596be2387f95b1a6314a05"


def _system_info(
    hostname: str = "DESKTOP-6VOR261",
    machine_id_hash: Optional[str] = _HASH,
    machine_id_type: str = "windows_machine_guid",
) -> Dict[str, Any]:
    return {
        "hostname": hostname,
        "machine_id_hash": machine_id_hash,
        "machine_id_type": machine_id_type,
    }


# ---------------------------------------------------------------------------
# 1. Derivarea
# ---------------------------------------------------------------------------

class DerivationTests(unittest.TestCase):
    def test_the_same_machine_always_derives_the_same_id(self) -> None:
        """
        Idempotența la reinstalare, exprimată direct.

        Dacă fișierul de identitate se pierde și agentul se reinstalează pe
        aceeași mașină, derivarea trebuie să producă exact același agent_id,
        deci aceeași înregistrare pe server. Un identificator aleator ar fi la
        fel de unic, dar ar crea o înregistrare nouă la fiecare reinstalare.
        """
        first = derive_agent_id("DESKTOP-6VOR261", _HASH)
        second = derive_agent_id("DESKTOP-6VOR261", _HASH)

        self.assertEqual(first, second)

    def test_the_readable_prefix_comes_from_the_hostname(self) -> None:
        agent_id = derive_agent_id("DESKTOP-6VOR261", _HASH)

        self.assertTrue(agent_id.startswith("desktop-6vor261-"))

    def test_the_deterministic_core_comes_from_the_machine_fingerprint(self) -> None:
        agent_id = derive_agent_id("HOST", _HASH)

        self.assertTrue(agent_id.endswith(_HASH[:8]))

    def test_two_machines_with_the_same_hostname_do_not_collide(self) -> None:
        """
        Cazul care a produs issue-ul: două mașini desfășurate din aceeași
        imagine, deci cu același hostname, dar cu amprente diferite.
        """
        first = derive_agent_id("HOST", "a" * 64)
        second = derive_agent_id("HOST", "b" * 64)

        self.assertNotEqual(first, second)


# ---------------------------------------------------------------------------
# 2. Normalizarea
# ---------------------------------------------------------------------------

class NormalizationTests(unittest.TestCase):
    def test_unsafe_characters_are_reduced(self) -> None:
        self.assertEqual(normalize_id_part("DESKTOP-6VOR261.local"), "desktop-6vor261-local")
        self.assertEqual(normalize_id_part("  Statia Gabi  "), "statia-gabi")
        self.assertEqual(normalize_id_part("mașina_de_test"), "ma-ina-de-test")
        self.assertEqual(normalize_id_part("---"), "")
        self.assertEqual(normalize_id_part(None), "")

    def test_a_derived_id_survives_url_quoting_unchanged(self) -> None:
        """
        agent_id intră într-o cale de URL (services/transport.py, send_heartbeat,
        quote(agent_id, safe="")). Dacă normalizarea ar lăsa un punct, un spațiu
        sau o diacritică, URL-ul ar conține secvențe procentuale, iar numele ar
        deveni imposibil de căutat în jurnale.
        """
        for hostname in ("DESKTOP-6VOR261.local", "mașină de test", "SRV_01"):
            with self.subTest(hostname=hostname):
                agent_id = derive_agent_id(hostname, _HASH)

                self.assertEqual(quote(agent_id, safe=""), agent_id)

    def test_a_hostname_that_normalizes_to_nothing_still_yields_an_id(self) -> None:
        agent_id = derive_agent_id("！！！", _HASH)

        self.assertEqual(agent_id, f"endpoint-{_HASH[:8]}")


# ---------------------------------------------------------------------------
# 3. Înghețarea
# ---------------------------------------------------------------------------

class FreezingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "agent_id"

    def test_the_first_run_derives_and_writes_the_identity(self) -> None:
        config: Dict[str, Any] = {}

        agent_id = resolve_agent_id(config, _system_info(), path=self.path)

        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_text(encoding="utf-8").strip(), agent_id)
        self.assertEqual(config["agent_id"], agent_id)

    def test_a_renamed_machine_keeps_its_frozen_identity(self) -> None:
        """
        Regresia centrală, și motivul pentru care înghețarea nu e opțională.

        Dacă identitatea s-ar recalcula la fiecare pornire, o redenumire de
        mașină ar produce un agent_id nou. Agentul ar trimite la register cheia
        veche împreună cu numele nou; secretul de înrolare fiind deja consumat,
        serverul ar cădea pe a doua cale de autorizare, ar găsi că acea cheie
        aparține numelui VECHI și ar răspunde 403. Iar 403 nu se repară singur
        niciodată: agentul ar rămâne blocat, cu coada crescând, până la o
        reprovizionare manuală pe acea mașină.
        """
        first = resolve_agent_id({}, _system_info(hostname="OLD-NAME"), path=self.path)
        second = resolve_agent_id({}, _system_info(hostname="BRAND-NEW-NAME"), path=self.path)

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("old-name-"))

    def test_a_machine_without_a_stable_fingerprint_still_gets_a_stable_id(self) -> None:
        """
        Fără amprentă, nucleul determinist lipsește și sufixul e aleator. Nu e o
        problemă: rezultatul se îngheață oricum, deci lipsa de determinism nu se
        mai vede după prima pornire.
        """
        info = _system_info(machine_id_hash=None, machine_id_type="unknown")

        first = resolve_agent_id({}, info, path=self.path)
        second = resolve_agent_id({}, info, path=self.path)

        self.assertEqual(first, second)

    def test_a_write_failure_does_not_stop_the_agent(self) -> None:
        """
        Un disc plin sau un director fără drepturi de scriere nu are voie să
        producă un agent care refuză să pornească. Identitatea ține pentru
        rularea curentă și se rederivează la următoarea — stabilă oricum, cât
        timp hostname-ul și amprenta nu s-au schimbat.
        """
        # Un fișier acolo unde ar trebui să fie un director: mkdir eșuează cu
        # FileExistsError, la fel ca pe un disc plin sau într-un director fără
        # drepturi de scriere, dar reproductibil pe orice platformă.
        blocker = self.path.with_name("blocker")
        blocker.write_text("sunt fisier, nu director", encoding="utf-8")
        config: Dict[str, Any] = {}

        with self.assertLogs("services.agent_identity", level="ERROR"):
            agent_id = resolve_agent_id(
                config, _system_info(), path=blocker / "agent_id"
            )

        self.assertTrue(agent_id)
        self.assertEqual(config["agent_id"], agent_id)

    def test_a_path_with_a_null_byte_is_survived_too(self) -> None:
        """
        agent_id_path vine din config.json, iar JSON poate purta un octet nul.
        os.mkdir ridică atunci ValueError, nu OSError — netratată, ar fi ieșit
        din resolve_agent_id și ar fi oprit agentul la pornire.
        """
        with self.assertLogs("services.agent_identity", level="ERROR"):
            agent_id = resolve_agent_id(
                {}, _system_info(), path=Path("\0invalid") / "agent_id"
            )

        self.assertTrue(agent_id)


# ---------------------------------------------------------------------------
# 4. Precedența
# ---------------------------------------------------------------------------

class PrecedenceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "agent_id"

    def test_an_explicit_config_value_wins_and_writes_nothing(self) -> None:
        """
        Cine fixează un nume în config.json vrea acel nume. Înghețarea unui al
        doilea nume pe lângă el ar crea două surse de adevăr, iar ștergerea
        cheii din config mai târziu ar produce o identitate surprinzătoare.
        """
        config: Dict[str, Any] = {"agent_id": "endpoint-01"}

        agent_id = resolve_agent_id(config, _system_info(), path=self.path)

        self.assertEqual(agent_id, "endpoint-01")
        self.assertFalse(self.path.exists())

    def test_the_frozen_file_wins_over_derivation(self) -> None:
        self.path.write_text("identitate-inghetata\n", encoding="utf-8")

        agent_id = resolve_agent_id({}, _system_info(), path=self.path)

        self.assertEqual(agent_id, "identitate-inghetata")

    def test_a_frozen_file_written_with_a_bom_is_read_cleanly(self) -> None:
        """Aceeași lecție ca la fișierele de credențiale, aplicată din start."""
        self.path.write_bytes("﻿identitate-cu-bom\n".encode("utf-8"))

        self.assertEqual(
            resolve_agent_id({}, _system_info(), path=self.path), "identitate-cu-bom"
        )

    def test_the_path_can_be_moved_from_config(self) -> None:
        moved = self.path.with_name("identitate-mutata")
        config: Dict[str, Any] = {"agent_id_path": str(moved)}

        agent_id = resolve_agent_id(config, _system_info())

        self.assertTrue(moved.exists())
        self.assertEqual(moved.read_text(encoding="utf-8").strip(), agent_id)

    def test_the_default_path_sits_next_to_the_credentials(self) -> None:
        """
        Nu în config.json, deliberat: acela e fișierul copiat între mașini, deci
        scrierea acolo ar reproduce exact bug-ul de la care s-a plecat.

        Ancora e agent_key, nu un nume de director: numele checkout-ului e o
        proprietate a mașinii pe care s-a clonat repo-ul (worktree, arhivă
        dezarhivată, alt director în CI), nu a codului. Ce trebuie să nu se
        schimbe tăcut e vecinătatea celor două fișiere.
        """
        self.assertEqual(DEFAULT_AGENT_ID_PATH.name, "agent_id")
        self.assertEqual(DEFAULT_AGENT_ID_PATH.parent, DEFAULT_AGENT_KEY_PATH.parent)

if __name__ == "__main__":
    unittest.main()
