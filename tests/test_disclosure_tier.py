"""
Teste pentru atribuirea pe trepte (contract v5).
=================================================

Blocul `disclosure` a fost declarat inainte sa existe treptele T1-T3, din
acelasi motiv pentru care `file_size` a fost impus cu doi pasi inainte sa
existe hashing: o atribuire de treapta nu se poate reconstrui retroactiv, iar
evenimentele scrise fara ea nu se pot separa mai tarziu de cele din epoca T0.

Testele de aici apara doua lucruri diferite. Primul: blocul chiar pleaca pe
fir, cu zero explicit la continut — un zero afirmat e verificabil, o absenta nu.
Al doilea: bicondiționalitatea `disclosure prezent <=> file_path prezent`, care
NU e validator pe server (ar sterge evenimente din spool ca poison messages),
deci traieste aici sau nicaieri.
"""

import unittest

import agent
from services.file_event import (
    DEFAULT_DISCLOSURE_TIER,
    VALID_DISCLOSURE_TIERS,
    _CONTENTLESS_TIERS,
    build_file_event_payload,
)
from tests.wire_contract import contract_model


def _payload(**kwargs):
    return build_file_event_payload(
        agent_id="agent-test",
        event_type="file_created",
        file_path="C:/tmp/proba.exe",
        agent_instance_id="incarnation-test",
        **kwargs,
    )


class DisclosureBlockTests(unittest.TestCase):
    def test_a_file_event_always_carries_a_tier(self) -> None:
        payload = _payload()

        self.assertIn("disclosure", payload)
        self.assertEqual(payload["disclosure"]["tier"], DEFAULT_DISCLOSURE_TIER)

    def test_content_bytes_is_emitted_as_an_explicit_zero(self) -> None:
        """
        Spre deosebire de campurile optionale, zero se emite. Un zero afirmat
        la T0 e o proprietate verificabila — niciun octet de continut n-a
        plecat — pe cand absenta ar fi ambigua intre 'zero' si 'nemasurat'.
        """
        payload = _payload()

        self.assertIn("content_bytes", payload["disclosure"])
        self.assertEqual(payload["disclosure"]["content_bytes"], 0)

    def test_the_default_tier_is_the_only_one_the_system_can_reach_today(
        self,
    ) -> None:
        """
        Regresie asupra unei minciuni posibile: daca cineva ridica valoarea
        implicita la T1 inainte sa existe motorul local de trasaturi, metrica
        ar raporta o treapta pe care sistemul nu o poate atinge.
        """
        self.assertEqual(DEFAULT_DISCLOSURE_TIER, "T0")

    def test_a_contentless_tier_refuses_to_carry_content(self) -> None:
        """
        Aceeasi invarianta e verificata si de schema serverului. Duplicarea e
        deliberata: aici esueaza la constructie, cu stack trace catre apelantul
        vinovat; acolo ar esua abia la 422, unde EventDispatcher trateaza
        raspunsul ca definitiv si sterge evenimentul din spool.
        """
        with self.assertRaises(ValueError) as raised:
            _payload(tier="T0", content_bytes=4096)

        self.assertIn("content_bytes", str(raised.exception))

    def test_an_escalated_tier_may_carry_content(self) -> None:
        payload = _payload(tier="T3", content_bytes=143_000)

        self.assertEqual(payload["disclosure"]["tier"], "T3")
        self.assertEqual(payload["disclosure"]["content_bytes"], 143_000)

    def test_a_tier_outside_the_vocabulary_is_refused_at_construction(self) -> None:
        """
        Oglinda trebuie să fie completă, nu parțială.

        Serverul respinge treapta necunoscută la validare; dacă agentul o lasă
        să treacă, răspunsul e 422 -> FatalTransportError, iar EventDispatcher
        tratează evenimentul ca poison message și îl șterge din spool. Verificat
        aici, eșecul e un stack trace către apelantul vinovat, nu un eveniment
        pierdut într-o rulare de producție.
        """
        with self.assertRaises(ValueError) as raised:
            _payload(tier="T4")

        self.assertIn("tier invalid", str(raised.exception))

    def test_negative_content_bytes_is_refused_at_construction(self) -> None:
        """A doua verificare pe care o face serverul și pe care agentul o datora."""
        with self.assertRaises(ValueError) as raised:
            _payload(tier="T3", content_bytes=-1)

        self.assertIn("negativ", str(raised.exception))


class TierVocabularyIsWrittenInTheContractTests(unittest.TestCase):
    """
    Vocabularul trăiește în trei locuri: frozenset-ul de aici, cel din schema
    serverului, și notele contractului. Fără testul ăsta, o treaptă adăugată
    într-unul și uitată în celelalte nu produce niciun eșec la rulare — produce
    doar două repo-uri care validează scale diferite, exact divergența pe care
    contractul de fir există s-o prevină. Aceeași disciplină ca la hash_status.
    """

    def _notes(self) -> str:
        return contract_model("event_disclosure")["notes"]

    def test_every_accepted_tier_is_documented_in_the_contract(self) -> None:
        notes = self._notes()
        undocumented = sorted(
            tier for tier in VALID_DISCLOSURE_TIERS if f"'{tier}'" not in notes
        )

        self.assertEqual(
            undocumented,
            [],
            f"Trepte acceptate de builder, dar nemenționate în notele "
            f"contractului: {undocumented}.",
        )

    def test_the_contentless_tiers_are_documented_in_the_contract(self) -> None:
        """
        Nu doar care trepte există, ci și care dintre ele nu au voie să poarte
        conținut: invarianta e verificată în trei locuri și trebuie să fie
        scrisă într-unul singur.
        """
        notes = self._notes()

        self.assertIn(
            "FARA CONTINUT",
            notes,
            "Contractul nu mai numește treptele fără conținut; invarianta "
            "verificată de builder și de schemă a rămas fără sursă scrisă.",
        )

        for tier in sorted(_CONTENTLESS_TIERS):
            with self.subTest(tier=tier):
                self.assertIn(f"'{tier}'", notes)


class BiconditionalityTests(unittest.TestCase):
    """
    `disclosure` prezent <=> `file_path` prezent.

    Nu e validator pe server, deliberat: un agent care inca nu emite blocul ar
    primi 422 -> FatalTransportError -> stergere din spool. Verificarea
    traieste deci aici, impotriva builderilor reali ai agentului, si va deveni
    validator cand toate caile o respecta — la fel cum s-a intamplat cu
    hash_status intre v3 si v4.
    """

    def test_file_events_carry_a_disclosure_block(self) -> None:
        payload = _payload()

        self.assertIsNotNone(payload.get("file_path"))
        self.assertIn("disclosure", payload)

    def test_lifecycle_events_carry_neither_file_path_nor_disclosure(
        self,
    ) -> None:
        """
        Scara T0-T3 descrie cat s-a divulgat DINTR-UN FISIER. Un eveniment de
        pornire nu divulga un fisier, ci starea agentului: o treapta acolo ar
        fi o eroare de categorie, iar un tier nul ar fi aceeasi ambiguitate
        imbracata in schema.
        """
        config = {
            "agent_id": "agent-test",
            "agent_instance_id": "11111111-2222-3333-4444-555555555555",
            "agent_version": "1.0.0",
        }

        builders = {
            "agent_startup": agent.build_startup_event_payload,
            "agent_shutdown": agent.build_shutdown_event_payload,
        }

        for name, builder in builders.items():
            with self.subTest(builder=name):
                payload = builder(config)

                self.assertIsNone(
                    payload.get("file_path"),
                    f"{name} a produs file_path — daca asta e intentionat, "
                    f"bicondiționalitatea cere si un bloc disclosure.",
                )
                self.assertNotIn(
                    "disclosure",
                    payload,
                    f"{name} nu e pe scara de divulgare si nu trebuie sa "
                    f"poarte o treapta.",
                )


if __name__ == "__main__":
    unittest.main()