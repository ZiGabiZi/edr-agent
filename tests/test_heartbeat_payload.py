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


if __name__ == "__main__":
    unittest.main()
