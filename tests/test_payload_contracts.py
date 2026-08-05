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

    Numele de câmpuri nu mai sunt copiate de mână din codul serverului: vin din
    contracts/wire-contract.json, exemplarul local al contractului comis identic
    în ambele repo-uri. Serverul își validează modelele Pydantic față de același
    fișier, deci o redenumire acolo pică testele acolo — nu peste luni, aici.
"""

import unittest

import agent
from services.file_monitor import build_file_event_payload
from tests.wire_contract import (
    CONTRACT,
    declared_fields,
    find_peer_repo,
    forbidden_fields,
    load_contract,
    required_fields,
)


# Câmpuri pe care contractul le cere, dar pe care nu le pune builder-ul: sunt
# adăugate mai jos în stivă, de stratul de transport. E o chestiune de stratificare
# internă a agentului, nu de protocol, deci nu are ce căuta în contract.
TRANSPORT_SUPPLIED_FIELDS = {
    # services/transport.py::send_heartbeat îl copiază din URL în corp.
    "heartbeat_request": frozenset({"agent_id"}),
}


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
    """(nume builder, payload emis, modelul din contract pe care îl țintește)."""
    config = _make_config()

    return [
        (
            "build_agent_registration_payload",
            agent.build_agent_registration_payload(config, _make_system_info()),
            "agent_register_request",
        ),
        (
            "build_startup_event_payload",
            agent.build_startup_event_payload(config),
            "event_create_request",
        ),
        (
            "build_shutdown_event_payload",
            agent.build_shutdown_event_payload(config),
            "event_create_request",
        ),
        (
            "build_file_event_payload",
            build_file_event_payload("agent-test", "file_created", "C:/tmp/proba.txt"),
            "event_create_request",
        ),
        (
            "build_heartbeat_payload",
            agent.build_heartbeat_payload(config, sequence=1),
            "heartbeat_request",
        ),
    ]


class PayloadContractTests(unittest.TestCase):
    """Fiecare builder trebuie să respecte modelul pe care îl țintește."""

    def test_no_builder_emits_a_key_the_server_would_silently_drop(self) -> None:
        for builder_name, payload, model_name in _all_builder_cases():
            with self.subTest(builder=builder_name):
                dropped = set(payload) - declared_fields(model_name)
                self.assertEqual(
                    dropped,
                    set(),
                    f"{builder_name} trimite chei nedeclarate de {model_name}: "
                    f"{sorted(dropped)}. Pydantic le aruncă tăcut, agentul primește "
                    f"200 OK, iar câmpurile rămân None pe server.",
                )

    def test_every_builder_supplies_the_fields_the_contract_makes_mandatory(self) -> None:
        """
        Cealaltă direcție: o cheie *ștearsă* din builder lasă payload-ul un subset
        perfect valid ca formă. Dacă acea cheie e obligatorie, serverul răspunde 422
        și payload-ul se pierde întreg — la prima rulare, nu la deploy.
        """
        for builder_name, payload, model_name in _all_builder_cases():
            with self.subTest(builder=builder_name):
                carried = set(payload) | TRANSPORT_SUPPLIED_FIELDS.get(
                    model_name, frozenset()
                )
                missing = required_fields(model_name) - carried

                self.assertEqual(
                    missing,
                    set(),
                    f"{builder_name} nu trimite câmpuri obligatorii în "
                    f"{model_name}: {sorted(missing)}. Serverul răspunde 422 și "
                    f"payload-ul se pierde întreg.",
                )

    def test_no_builder_emits_a_field_the_contract_forbids(self) -> None:
        """
        Câmpurile interzise nu sunt curățenie: fiecare are în contract, la 'notes',
        invarianta pe care prezența lui o rupe. Vezi agent_instance_id în
        agent_register_request — trimis acolo, dezactivează complet detecția de
        repornire, fără nicio eroare vizibilă.
        """
        for builder_name, payload, model_name in _all_builder_cases():
            with self.subTest(builder=builder_name):
                emitted_forbidden = set(payload) & forbidden_fields(model_name)

                self.assertEqual(
                    emitted_forbidden,
                    set(),
                    f"{builder_name} emite câmpuri interzise de {model_name}: "
                    f"{sorted(emitted_forbidden)}. Motivul interdicției e în "
                    f"contract, la models.{model_name}.notes.",
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

        Regula e codificată și în contract (forbidden), iar serverul o verifică pe
        modelul lui. Testul de față o păstrează pe payload-ul real al agentului.
        """
        payload = agent.build_agent_registration_payload(
            _make_config(), _make_system_info()
        )

        self.assertNotIn(
            "agent_instance_id",
            payload,
            "Incarnarea aparține exclusiv canalului de heartbeat.",
        )

    def test_the_contract_still_forbids_the_incarnation_at_registration(self) -> None:
        """
        Gardă pentru gardă: testul de mai sus își pierde sensul dacă cineva scoate
        interdicția din contract și adaugă câmpul în schema serverului. Payload-ul
        agentului ar rămâne curat, dar invarianta ar fi deja pierdută.
        """
        self.assertIn(
            "agent_instance_id",
            forbidden_fields("agent_register_request"),
            "Interdicția a dispărut din contract. Dacă e intenționat, citește mai "
            "întâi models.agent_register_request.notes: fără ea, restart_detected "
            "nu mai devine True niciodată.",
        )


class ContractSyncTests(unittest.TestCase):
    """
    Cele două exemplare ale contractului trebuie să fie identice.

    Contractul își câștigă rostul doar dacă ambele repo-uri validează *același*
    text. Un exemplar actualizat pe o singură parte readuce exact problema pe care
    fișierul o rezolvă — doar că mai greu de observat, pentru că acum arată ca un
    mecanism care funcționează.
    """

    def test_the_peer_repository_carries_the_same_contract(self) -> None:
        peer_repo = find_peer_repo()

        if peer_repo is None:
            self.skipTest(
                "edr-server nu a fost găsit lângă agent; sincronizarea celor două "
                "exemplare rămâne neverificată în această rulare. Setează "
                "EDR_SERVER_PATH pentru a o verifica."
            )

        peer_contract = load_contract(peer_repo)

        # Comparăm JSON-ul parsat, nu octeții: repo-urile sunt clonate și pe
        # Windows, și prin WSL, iar o diferență CRLF/LF ar produce eșecuri false
        # care n-au nicio legătură cu numele de pe fir.
        self.assertEqual(
            peer_contract.get("contract_version"),
            CONTRACT["contract_version"],
            f"Exemplarele de contract sunt la versiuni diferite (local "
            f"v{CONTRACT['contract_version']}, {peer_repo.name} "
            f"v{peer_contract.get('contract_version')}). Copiază versiunea nouă în "
            f"repo-ul rămas în urmă — până atunci, cele două părți validează reguli "
            f"diferite.",
        )

        # Aceeași versiune, conținut diferit: cineva a editat un exemplar fără să
        # incrementeze. Raportăm modelele care diferă, nu întregul JSON.
        local_models = CONTRACT["models"]
        peer_models = peer_contract["models"]
        differing_models = sorted(
            name
            for name in set(local_models) | set(peer_models)
            if local_models.get(name) != peer_models.get(name)
        )

        self.assertEqual(
            differing_models,
            [],
            f"Exemplarele au aceeași versiune, dar descriu diferit modelele: "
            f"{differing_models}. Un exemplar a fost editat fără să fie propagat.",
        )

        self.assertEqual(
            peer_contract,
            CONTRACT,
            "Exemplarele diferă în afara secțiunii 'models'.",
        )


if __name__ == "__main__":
    unittest.main()
