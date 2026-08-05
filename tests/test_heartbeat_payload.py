"""
Teste de contract pentru payload-ul de heartbeat construit de agent.

De ce există acest fișier:
    Detecția de repornire de pe server compară incarnarea raportată de agent
    (agent_instance_id) cu ultima incarnare cunoscută. Serverul validează corpul
    cererii cu Pydantic, iar Pydantic *ignoră în tăcere* cheile pe care nu le
    cunoaște. Consecința: o cheie scrisă greșit în agent nu produce nicio eroare
    — nici la agent, nici la server — dar câmpul ajunge None pe server și
    detecția nu se declanșează niciodată.

    Testele de pe server (app/tests/test_heartbeat_sequence.py) construiesc
    payload-ul manual, cu numele corecte, deci trec chiar și atunci când agentul
    real trimite altceva. Golul se închide doar dintr-o parte: verificând că
    payload-ul emis de agent folosește exact numele de câmpuri citite de server.
"""

import threading
import unittest
from unittest.mock import patch

import agent
from services.transport import TransportError


# Câmpurile declarate de HeartbeatRequest în edr-server/app/schemas/heartbeat.py.
# Orice cheie din payload care nu se află aici este ignorată tăcut de Pydantic.
# "agent_id" nu e produs de builder — îl adaugă transport.send_heartbeat().
SERVER_HEARTBEAT_FIELDS = frozenset(
    {"agent_id", "agent_version", "sequence", "agent_instance_id"}
)


def _make_config() -> dict:
    return {
        "agent_id": "agent-test",
        "agent_instance_id": "11111111-2222-3333-4444-555555555555",
        "agent_version": "1.0.0",
    }


class HeartbeatPayloadContractTests(unittest.TestCase):
    """Payload-ul emis de agent trebuie să folosească numele citite de server."""

    def test_payload_carries_instance_id_under_the_key_the_server_reads(self) -> None:
        config = _make_config()

        payload = agent.build_heartbeat_payload(config, sequence=1)

        self.assertIn("agent_instance_id", payload)
        self.assertEqual(
            payload["agent_instance_id"], config["agent_instance_id"]
        )

    def test_payload_has_no_key_the_server_would_silently_drop(self) -> None:
        payload = agent.build_heartbeat_payload(_make_config(), sequence=1)

        unknown_keys = set(payload) - SERVER_HEARTBEAT_FIELDS
        self.assertEqual(
            unknown_keys,
            set(),
            "Chei necunoscute de HeartbeatRequest — Pydantic le aruncă, "
            "iar câmpul corespunzător rămâne None pe server.",
        )

    def test_payload_carries_the_sequence_it_was_given(self) -> None:
        payload = agent.build_heartbeat_payload(_make_config(), sequence=7)

        self.assertEqual(payload["sequence"], 7)


class HeartbeatLoopSequenceTests(unittest.TestCase):
    """Secvența crește o dată per *încercare*, inclusiv peste eșecuri de rețea."""

    def test_loop_sends_incrementing_sequence_with_instance_id(self) -> None:
        config = _make_config()
        stop_event = threading.Event()
        sent_payloads: list = []

        def fake_send_heartbeat(server_url, agent_id, heartbeat_payload):
            sent_payloads.append(dict(heartbeat_payload))

            if len(sent_payloads) == 2:
                # Heartbeat pierdut: secvența trebuie să avanseze oricum, altfel
                # serverul nu ar putea număra heartbeat-urile lipsă.
                raise TransportError("server indisponibil")

            if len(sent_payloads) == 3:
                stop_event.set()

            return {"status": "ok", "directive": {"action": "none"}}

        with patch.object(agent, "send_heartbeat", fake_send_heartbeat), \
                patch.object(agent, "logger"):
            agent.heartbeat_loop(
                config=config,
                server_url="http://127.0.0.1:8000",
                system_info={},
                heartbeat_interval_seconds=0.01,
                stop_event=stop_event,
            )

        self.assertEqual(
            [payload["sequence"] for payload in sent_payloads], [1, 2, 3]
        )
        self.assertEqual(
            [payload["agent_instance_id"] for payload in sent_payloads],
            [config["agent_instance_id"]] * 3,
        )

class IncarnationOwnershipTests(unittest.TestCase):
    """
    ensure_agent_instance_id este singurul producător al incarnării.

    Testele de mai jos păzesc exact golul care făcea posibil eșecul tăcut:
    nimic nu verifica ce se întâmplă când config nu conține deloc cheia, pentru
    că singurul apelant real (run_agent) o injecta corect. Un refactor care mută
    sau elimină acea injecție ar fi trecut suita fără nicio alarmă.
    """

    def test_populates_the_incarnation_when_config_has_none(self) -> None:
        config = {"agent_id": "agent-test"}

        instance_id = agent.ensure_agent_instance_id(config)

        self.assertTrue(instance_id)
        self.assertEqual(config["agent_instance_id"], instance_id)

    def test_repeated_calls_keep_the_same_incarnation(self) -> None:
        """O a doua incarnare în aceeași rulare ar fi citită drept repornire falsă."""
        first = agent.ensure_agent_instance_id({"agent_id": "agent-test"})
        second = agent.ensure_agent_instance_id({"agent_id": "agent-test"})

        self.assertEqual(first, second)

    def test_a_value_coming_from_config_json_is_replaced(self) -> None:
        """
        O incarnare fixată pe disc ar fi identică la fiecare pornire: serverul
        n-ar mai vedea nicio schimbare, deci nicio repornire n-ar mai fi detectată.
        """
        config = {"agent_id": "agent-test", "agent_instance_id": "fixat-in-config-json"}

        with patch.object(agent, "logger"):
            instance_id = agent.ensure_agent_instance_id(config)

        self.assertNotEqual(instance_id, "fixat-in-config-json")
        self.assertEqual(config["agent_instance_id"], instance_id)


class PayloadStrictnessTests(unittest.TestCase):
    """Un config fără incarnare oprește construcția, nu produce un payload cu None."""

    def test_heartbeat_builder_refuses_a_config_without_incarnation(self) -> None:
        config = _make_config()
        del config["agent_instance_id"]

        with self.assertRaises(ValueError):
            agent.build_heartbeat_payload(config, sequence=1)

    def test_heartbeat_builder_refuses_a_blank_incarnation(self) -> None:
        config = _make_config()
        config["agent_instance_id"] = "   "

        with self.assertRaises(ValueError):
            agent.build_heartbeat_payload(config, sequence=1)

    def test_lifecycle_builders_refuse_a_config_without_incarnation(self) -> None:
        """
        Evenimentele de ciclu de viață sunt corelate pe server prin incarnare;
        un None acolo rupe legătura dintre agent_startup și rularea care l-a emis.
        """
        config = _make_config()
        del config["agent_instance_id"]

        for builder in (
            agent.build_startup_event_payload,
            agent.build_shutdown_event_payload,
        ):
            with self.subTest(builder=builder.__name__):
                with self.assertRaises(ValueError):
                    builder(config)


if __name__ == "__main__":
    unittest.main()
