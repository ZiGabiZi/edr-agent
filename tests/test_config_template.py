"""
Șablonul livrat — artefactul, nu codul.
=======================================

Restul suitei verifică ce face config_loader cu o configurare dată. Aici se
verifică fișierul pe care îl primește efectiv cineva care clonează repo-ul,
pentru că exact acolo a stat bug-ul: codul știa să derive un agent_id, dar
șablonul livrat conținea unul scris fix, deci derivarea nu rula niciodată la
o instalare reală. O reparație prezentă în cod și absentă în practică.

E singurul loc din suită care are voie să citească fișiere din rădăcina
repo-ului: ele SUNT subiectul testului.
"""

import json
import unittest
from pathlib import Path

from services.config_loader import CONFIG_PATH, load_config


_BASE_DIR = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG_PATH = _BASE_DIR / "config.example.json"
GITIGNORE_PATH = _BASE_DIR / ".gitignore"


class ShippedTemplateTests(unittest.TestCase):
    def test_the_template_exists_next_to_the_config_it_seeds(self) -> None:
        self.assertTrue(
            EXAMPLE_CONFIG_PATH.exists(),
            f"{EXAMPLE_CONFIG_PATH.name} lipsește: fără el, mesajul de eroare "
            "din load_config nu mai are ce recomanda unui clone proaspăt.",
        )
        self.assertEqual(EXAMPLE_CONFIG_PATH.parent, CONFIG_PATH.parent)

    def test_the_template_does_not_freeze_an_identity(self) -> None:
        """
        Cheia asta în șablon anulează derivarea din services/agent_identity.py:
        a doua mașină instalată din același fișier ar revendica numele primei,
        iar serverul ar respinge înregistrarea cu 409.
        """
        template = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))

        self.assertNotIn(
            "agent_id",
            template,
            "Șablonul livrat impune un agent_id. Scoate cheia: absentă, "
            "identitatea e derivată din amprenta mașinii și înghețată local.",
        )

    def test_the_template_survives_the_real_validator(self) -> None:
        """
        Nu doar „e JSON valid": trece exact prin drumul pe care îl parcurge
        configurarea reală la pornire. Un șablon care a rămas în urma unei
        validări noi ar da, la primul clone, o eroare pe care autorul
        schimbării n-a văzut-o niciodată.
        """
        config = load_config(EXAMPLE_CONFIG_PATH)

        self.assertNotIn("agent_id", config)
        self.assertTrue(config["server_url"])

    def test_the_machine_config_stays_out_of_version_control(self) -> None:
        """
        Perechea șablon/local nu ține singură: dacă regula de ignorare dispare,
        config.json redevine versionat și, odată cu el, valorile unei singure
        mașini redevin implicitul tuturor.
        """
        ignored = GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()
        stripped = [line.strip() for line in ignored]

        self.assertIn(
            "/config.json",
            stripped,
            "Regula /config.json a dispărut din .gitignore.",
        )
        self.assertNotIn(
            "/config.example.json",
            stripped,
            "Șablonul trebuie să rămână versionat: el e artefactul livrat.",
        )


if __name__ == "__main__":
    unittest.main()