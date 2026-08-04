"""
Teste de contract pentru TOATE payload-urile emise de agent.

De ce există acest fișier:
    Schemele Pydantic de pe server rulează cu extra="ignore". O cheie trimisă de
    agent și nedeclarată în schemă este aruncată tăcut: agentul primește 200 OK,
    serverul nu loghează nimic, iar câmpul rămâne None. Bug-ul nu se manifestă ca
    eroare, ci ca date care lipsesc luni mai târziu, când cineva le caută.

    tests/test_heartbeat_payload.py acoperea doar build_heartbeat_payload, iar în
    acest timp build_startup_event_payload trimitea agent_instance_id și occurred_at
    către o schemă care nu le declara. Fișierul de față generalizează verificarea la
    toți builderii, ca a patra instanță a aceleiași greșeli să nu mai poată apărea.
"""

import unittest

import agent
from services.file_monitor import build_file_event_payload


# ---------------------------------------------------------------------------
# Oglinda schemelor serverului (edr-server/app/schemas/*.py)
#
# Repo-urile fiind separate, seturile de mai jos sunt duplicate manual. Când o
# schemă de pe server se schimbă, setul corespunzător trebuie actualizat în
# aceeași modificare — altfel testul devine o garanție falsă.
# ---------------------------------------------------------------------------

# AgentRegisterRequest
SERVER_REGISTER_FIELDS = frozenset({
    "agent_id",
    "agent_version",
    "hostname",
    "operating_system",
    "architecture",
    "os_architecture",
    "machine_id_type",
    "machine_id_hash",
    "ip_address",
})

# EventCreateRequest
SERVER_EVENT_FIELDS = frozenset({
    "agent_id",
    "agent_instance_id",
    "event_type",
    "client_event_id",
    "file_path",
    "sha256",
    "description",
    "occurred_at",
})

# HeartbeatRequest. "agent_id" nu e produs de builder — îl adaugă
# transport.send_heartbeat() înainte de POST.
SERVER_HEARTBEAT_FIELDS = frozenset({
    "agent_id",
    "agent_version",
    "sequence",
    "agent_instance_id",
})


def _make_config() -> dict:
    return {
        "agent_id": "agent-test",
        "agent_instance_id": "11111111-2222-3333-4444-555555555555",
        "agent_version": "1.0.0",
    }


def _make_system_info() -> dict:
    return {
        "hostname": "HOST-TEST",
        "operating_system": "windows",
        "ip_address": "192.168.1.10",
        "architecture": "x64",
        "os_architecture": "x64",
        "machine_id_type": "hash",
        "machine_id_hash": "abcdef",
    }


def _all_builder_cases() -> list:
    """(nume, payload, câmpuri acceptate de schema serverului) pentru fiecare builder."""
    config = _make_config()

    return [
        (
            "build_agent_registration_payload",
            agent.build_agent_registration_payload(config, _make_system_info()),
            SERVER_REGISTER_FIELDS,
        ),
        (
            "build_startup_event_payload",
            agent.build_startup_event_payload(config),
            SERVER_EVENT_FIELDS,
        ),
        (
            "build_shutdown_event_payload",
            agent.build_shutdown_event_payload(config),
            SERVER_EVENT_FIELDS,
        ),
        (
            "build_file_event_payload",
            build_file_event_payload("agent-test", "file_created", "C:/tmp/proba.txt"),
            SERVER_EVENT_FIELDS,
        ),
        (
            "build_heartbeat_payload",
            agent.build_heartbeat_payload(config, sequence=1),
            SERVER_HEARTBEAT_FIELDS,
        ),
    ]


class PayloadContractTests(unittest.TestCase):
    """Niciun builder nu are voie să emită o cheie pe care serverul o aruncă."""

    def test_no_builder_emits_a_key_the_server_would_silently_drop(self) -> None:
        for builder_name, payload, allowed_fields in _all_builder_cases():
            with self.subTest(builder=builder_name):
                dropped = set(payload) - allowed_fields
                self.assertEqual(
                    dropped,
                    set(),
                    f"{builder_name} trimite chei nedeclarate în schema serverului: "
                    f"{sorted(dropped)}. Pydantic le aruncă tăcut, agentul primește "
                    f"200 OK, iar câmpurile rămân None pe server.",
                )

    def test_every_event_builder_reports_when_the_event_occurred(self) -> None:
        """
        Fără occurred_at, singura ordonare posibilă pe server este event_id, adică
        ordinea de livrare. Cu spool persistent, un eveniment produs în timpul unei
        pene de rețea se livrează după evenimente mai noi — deci ordinea de livrare
        contrazice ordinea reală exact în situațiile care contează.
        """
        config = _make_config()
        event_payloads = {
            "startup": agent.build_startup_event_payload(config),
            "shutdown": agent.build_shutdown_event_payload(config),
            "file": build_file_event_payload("agent-test", "file_created", "C:/tmp/proba.txt"),
        }

        for name, payload in event_payloads.items():
            with self.subTest(event=name):
                self.assertTrue(
                    payload.get("occurred_at"),
                    f"Evenimentul '{name}' nu raportează momentul producerii.",
                )


class LifecycleEventCorrelationTests(unittest.TestCase):
    """Evenimentele de ciclu de viață poartă incarnarea rulării curente."""

    def test_startup_and_shutdown_carry_the_same_incarnation(self) -> None:
        config = _make_config()

        startup = agent.build_startup_event_payload(config)
        shutdown = agent.build_shutdown_event_payload(config)

        self.assertEqual(startup["agent_instance_id"], config["agent_instance_id"])
        self.assertEqual(shutdown["agent_instance_id"], config["agent_instance_id"])

    def test_startup_matches_the_incarnation_reported_by_heartbeats(self) -> None:
        """
        Rostul întregii schimbări: un agent_startup trebuie să poată fi legat de
        rularea ale cărei heartbeat-uri raportează aceeași incarnare.
        """
        config = _make_config()

        startup = agent.build_startup_event_payload(config)
        heartbeat = agent.build_heartbeat_payload(config, sequence=1)

        self.assertEqual(startup["agent_instance_id"], heartbeat["agent_instance_id"])


class RegistrationPayloadTests(unittest.TestCase):
    """Înregistrarea nu are voie să transporte incarnarea."""

    def test_registration_must_not_carry_the_incarnation(self) -> None:
        """
        Nu e o simplă curățenie: la repornire, înregistrarea rulează înaintea primului
        heartbeat, iar register_agent() de pe server face update() peste înregistrarea
        existentă. O incarnare trimisă la înregistrare ar suprascrie baseline-ul înainte
        ca heartbeat-ul să îl poată compara, iar restart_detected n-ar mai fi True
        niciodată. Dacă acest test cade, detecția de repornire e moartă — indiferent
        cât de corect arată codul din agent_service.
        """
        payload = agent.build_agent_registration_payload(
            _make_config(), _make_system_info()
        )

        self.assertNotIn(
            "agent_instance_id",
            payload,
            "Incarnarea aparține exclusiv canalului de heartbeat.",
        )


if __name__ == "__main__":
    unittest.main()