"""
Teste de contract pentru payload-ul de heartbeat construit de agent.

De ce există acest fișier:
    Detecția de repornire de pe server compară incarnarea raportată de agent
    (agent_instance_id) cu ultima incarnare cunoscută. Serverul validează corpul
    cererii cu Pydantic, care aruncă cheile pe care schema nu le declară.
    Consecința: o cheie scrisă greșit în agent nu oprește nimic — nici la agent,
    nici la server — dar câmpul ajunge None pe server și detecția nu se
    declanșează niciodată. Serverul loghează acum cheia necunoscută
    (app/schemas/wire.py), dar abia după ce payload-ul greșit a plecat deja
    într-o rulare reală.

    Testele de pe server (app/tests/test_heartbeat_sequence.py) construiesc
    payload-ul manual, cu numele corecte, deci trec chiar și atunci când agentul
    real trimite altceva. Golul se închide doar dintr-o parte: verificând că
    payload-ul emis de agent folosește exact numele de câmpuri citite de server.

    Numele acelea vin din contracts/wire-contract.json, nu dintr-o copie a
    schemei ținută aici. Fișierul de contract e comis identic în ambele repo-uri
    și validat de amândouă (vezi tests/wire_contract.py).
"""

import threading
import unittest
from contextlib import contextmanager, ExitStack
from unittest.mock import patch

import agent
from services.transport import TransportError
from tests.wire_contract import declared_fields


# Câmpurile pe care build_heartbeat_payload se angajează să le producă.
# "agent_id" lipsește intenționat: îl adaugă transport.send_heartbeat().
BUILDER_HEARTBEAT_FIELDS = frozenset(
    {"agent_version", "sequence", "agent_instance_id"}
)

# Câmpurile din răspuns pe care heartbeat_loop le citește efectiv. Sunt
# hardcodate în agent.py, deci o redenumire pe server le-ar lăsa să întoarcă
# None fără nicio eroare — testele de la finalul fișierului le leagă de contract.
NEXT_INTERVAL_FIELD = "next_heartbeat_seconds"
DIRECTIVE_FIELD = "directive"
DIRECTIVE_ACTION_FIELD = "action"


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

    def test_payload_contains_exactly_the_fields_the_agent_commits_to_send(self) -> None:
        """
        Egalitate, nu doar „nicio cheie în plus".

        Verificarea unidirecțională prinde greșelile de scriere, dar nu și
        ștergerile: un câmp scos din builder lasă payload-ul un subset perfect
        valid, iar Pydantic îi pune None pe server fără nicio eroare. Egalitatea
        închide și direcția asta.
        """
        payload = agent.build_heartbeat_payload(_make_config(), sequence=1)

        self.assertEqual(
            set(payload),
            BUILDER_HEARTBEAT_FIELDS,
            "Payload-ul de heartbeat nu mai conține exact câmpurile declarate. "
            "Dacă schimbarea e intenționată, actualizează BUILDER_HEARTBEAT_FIELDS "
            "împreună cu builder-ul.",
        )

    def test_every_field_the_agent_sends_is_known_to_the_server(self) -> None:
        """
        Cealaltă jumătate a contractului: tot ce pleacă de la agent trebuie să
        existe în heartbeat_request. Incluziune, nu egalitate — contractul are
        voie să declare câmpuri opționale pe care agentul nu le trimite încă.
        """
        sent_fields = BUILDER_HEARTBEAT_FIELDS | {"agent_id"}

        self.assertEqual(
            sent_fields - declared_fields("heartbeat_request"),
            set(),
            "Chei necunoscute de HeartbeatRequest — Pydantic le aruncă, "
            "iar câmpul corespunzător rămâne None pe server.",
        )

    def test_payload_carries_the_sequence_it_was_given(self) -> None:
        payload = agent.build_heartbeat_payload(_make_config(), sequence=7)

        self.assertEqual(payload["sequence"], 7)


# Ușile către rețea importate în agent.py. heartbeat_loop poate ajunge la
# check_server_health și register_agent fără să le numească vreodată direct:
# și directiva "reregister", și ramura AgentNotRegisteredError trec prin
# register_agent_with_retry, care le apelează pe amândouă.
_TRANSPORT_ENTRY_POINTS = ("check_server_health", "register_agent", "send_event")


def _refuse_network_call(name: str):
    """Înlocuiește o funcție de transport care nu are voie să fie atinsă."""

    def refuse(*args, **kwargs):
        raise AssertionError(
            f"{name}() a fost apelat în timpul unui test de buclă. Bucla a ajuns "
            "pe o cale care iese în rețea (directivă 'reregister' sau "
            "AgentNotRegisteredError -> register_agent_with_retry). Dacă acea cale "
            "este chiar subiectul testului, înlocuiește "
            "agent.register_agent_with_retry cu un dublu; altfel corectează "
            "răspunsul fals, ca bucla să nu mai ajungă acolo."
        )

    return refuse


@contextmanager
def _loop_isolated_from_the_network(fake_send_heartbeat):
    """
    Rulează heartbeat_loop cu un singur punct de contact fals: send_heartbeat.

    De ce nu e suficient să înlocuiești doar send_heartbeat:
        heartbeat_loop are două ramuri care cheamă register_agent_with_retry —
        directiva "reregister" și AgentNotRegisteredError. Bucla aceea folosește
        check_server_health și register_agent, care ar rămâne cele reale. Un
        răspuns fals schimbat mai târziu ar transforma testul unitar în trafic
        HTTP: fie reîncercări la nesfârșit contra unui port închis (bucla de
        startup nu abandonează niciodată din cauza numărului de încercări, iar
        stop_event se setează doar din send_heartbeat, care nu mai e apelat), fie
        — dacă serverul de dezvoltare chiar ascultă pe acel port — o înregistrare
        reală, cu scriere în baza lui de date.

        În ambele cazuri rezultatul testului ar depinde de ce rulează pe mașina
        celui care îl execută. Sigilarea îl face să eșueze imediat, cu motivul
        scris în mesaj, în loc să atârne sau să atingă un server real.
    """
    with ExitStack() as patches:
        patches.enter_context(
            patch.object(agent, "send_heartbeat", fake_send_heartbeat)
        )
        patches.enter_context(patch.object(agent, "logger"))

        for name in _TRANSPORT_ENTRY_POINTS:
            patches.enter_context(
                patch.object(agent, name, _refuse_network_call(name))
            )

        yield




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

        with _loop_isolated_from_the_network(fake_send_heartbeat):
            agent.heartbeat_loop(
                config=_make_config(),
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


class _RecordingStopEvent(threading.Event):
    """Event care notează cât i s-a cerut să aștepte, fără să aștepte efectiv."""

    def __init__(self) -> None:
        super().__init__()
        self.wait_timeouts: list = []

    def wait(self, timeout=None) -> bool:
        self.wait_timeouts.append(timeout)
        return super().wait(0)


class HeartbeatResponseContractTests(unittest.TestCase):
    """
    Direcția inversă a contractului: ce *citește* agentul din răspuns.

    Modul de eșec e simetric și la fel de tăcut. Agentul citește răspunsul cu
    response.get(...), deci un câmp redenumit pe server nu produce nicio eroare
    — doar un None ignorat. Concret, o redenumire a lui next_heartbeat_seconds
    ar lăsa agentul pe intervalul lui local pentru totdeauna, iar cadența
    dictată central ar înceta să funcționeze fără ca cineva să observe.
    """

    def test_the_contract_declares_every_response_field_the_agent_reads(self) -> None:
        response_fields = declared_fields("heartbeat_response")
        directive_fields = declared_fields("heartbeat_directive")

        self.assertIn(
            NEXT_INTERVAL_FIELD,
            response_fields,
            "Agentul citește acest câmp în heartbeat_loop; contractul nu îl mai "
            "declară. Fie a fost redenumit pe server, fie agentul citește un nume "
            "mort — în ambele cazuri, cadența dictată de server nu mai ajunge.",
        )
        self.assertIn(DIRECTIVE_FIELD, response_fields)
        self.assertIn(
            DIRECTIVE_ACTION_FIELD,
            directive_fields,
            "Fără acest câmp, agentul crede permanent că serverul nu are nimic "
            "de cerut — inclusiv reînregistrarea.",
        )

    def test_loop_adopts_the_cadence_the_server_dictates(self) -> None:
        """
        Verificarea de nume de mai sus arată doar că numele *există*. Aici se
        confirmă că agentul chiar îl folosește: răspunsul e construit cu numele
        din contract, iar intervalul cerut trebuie să ajungă în așteptare.
        """
        stop_event = _RecordingStopEvent()
        server_dictated_interval = 42

        def fake_send_heartbeat(server_url, agent_id, heartbeat_payload):
            stop_event.set()
            return {
                "status": "ok",
                "agent_id": agent_id,
                DIRECTIVE_FIELD: {DIRECTIVE_ACTION_FIELD: "none"},
                NEXT_INTERVAL_FIELD: server_dictated_interval,
            }

        with _loop_isolated_from_the_network(fake_send_heartbeat):
            agent.heartbeat_loop(
                config=_make_config(),
                server_url="http://127.0.0.1:8000",
                system_info={},
                heartbeat_interval_seconds=5,
                stop_event=stop_event,
            )

        self.assertEqual(
            stop_event.wait_timeouts,
            [server_dictated_interval],
            "Agentul a rămas pe intervalul local. Câmpul din răspuns nu mai e "
            "citit sub numele pe care îl declară contractul.",
        )


if __name__ == "__main__":
    unittest.main()
