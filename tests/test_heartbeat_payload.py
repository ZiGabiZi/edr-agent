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
from services.transport import TransportError, AgentNotRegisteredError
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


# După cât timp este considerată blocată o rulare de heartbeat_loop dintr-un
# test. Nu este o măsură a cât durează testele — toate se termină în
# milisecunde — ci pragul de la care singura explicație rămasă este că bucla nu
# mai are cum să iasă singură.
_LOOP_WATCHDOG_SECONDS = 5.0


class _LoopWatchdog:
    """
    Oprește forțat heartbeat_loop atunci când testul nu mai reușește să o oprească.

    De ce există:
        Fiecare test de buclă își programează singur ieșirea din interiorul
        dublului de send_heartbeat: la a n-a apelare, dublul setează stop_event.
        Condiția aceea depinde însă chiar de comportamentul aflat sub test, deci
        o regresie o poate face să nu se împlinească niciodată — un `continue`
        strecurat înaintea lui send_heartbeat, o gardă care suprimă trimiterile
        cât timp agentul e degradat, o excepție nouă prinsă mai sus. Atunci
        nimeni nu mai setează stop_event, iar `while not stop_event.is_set()` se
        învârte la nesfârșit: suita nu eșuează, ci atârnă până la timeout-ul de
        CI sau până la Ctrl+C.

        Un defect care se manifestă ca blocaj este cel mai scump mod de a afla
        ceva: nu are mesaj, nu are stack trace și nu spune nici măcar ce test
        l-a provocat. Cronometrul de aici îl transformă într-un eșec obișnuit,
        cu explicație.

    De ce cronometru și nu un plafon de apeluri în dublu:
        Un contor pus în fake_send_heartbeat ar prinde doar bucla care trimite
        prea des. Regresia inversă — bucla care nu mai trimite deloc — nu ar
        ajunge niciodată să atingă contorul, adică exact cazul care produce
        blocajul. Cronometrul le acoperă pe amândouă, pentru că măsoară singurul
        lucru comun: faptul că bucla nu s-a mai oprit.

    Cum ajunge oprirea la buclă:
        heartbeat_loop nu doarme cu time.sleep(), ci cu stop_event.wait(timeout),
        deci un set() venit din alt fir o trezește imediat, oricât de mare ar fi
        intervalul cerut. Aceeași proprietate care face agentul să răspundă la
        Ctrl+C în mijlocul unei pauze de 300 de secunde face și plasa asta să
        funcționeze.
    """

    def __init__(self, stop_event, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self.fired = False
        self._stop_event = stop_event
        self._timer = threading.Timer(timeout_seconds, self._fire)
        # Fir daemon: dacă totuși rămâne în urmă, nu ține procesul de teste în
        # viață după ce raportul a fost scris.
        self._timer.daemon = True

    def _fire(self) -> None:
        self.fired = True
        self._stop_event.set()

    def start(self) -> None:
        self._timer.start()

    def cancel(self) -> None:
        """Oprirea cronometrului e idempotentă: cancel() după declanșare nu face nimic."""
        self._timer.cancel()

    def failure_message(self) -> str:
        return (
            f"heartbeat_loop nu s-a oprit singură în {self.timeout_seconds:g}s și a "
            "fost oprită forțat de plasa de siguranță a testului. Bucla nu a mai "
            "ajuns la condiția care setează stop_event: cel mai probabil nu mai "
            "apelează send_heartbeat pe calea verificată aici, sau îl apelează de "
            "mai multe ori decât se aștepta dublul. Fără această plasă, testul ar "
            "fi atârnat până la timeout-ul de CI, fără niciun mesaj."
        )


@contextmanager
def _loop_isolated_from_the_network(
    fake_send_heartbeat,
    stop_event,
    watchdog_seconds: float = _LOOP_WATCHDOG_SECONDS,
):
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

    De ce cere și stop_event:
        Ca plasa de siguranță (_LoopWatchdog) să nu fie opțională. Orice test de
        buclă trece oricum prin harness-ul acesta ca să nu iasă în rețea, deci
        primind aici evenimentul de oprire capătă și cronometrul, fără să ceară
        nimic. Un test nou nu are cum să uite să și-l pună.
    """
    watchdog = _LoopWatchdog(stop_event, watchdog_seconds)

    try:
        with ExitStack() as patches:
            patches.enter_context(
                patch.object(agent, "send_heartbeat", fake_send_heartbeat)
            )
            patches.enter_context(patch.object(agent, "logger"))

            for name in _TRANSPORT_ENTRY_POINTS:
                patches.enter_context(
                    patch.object(agent, name, _refuse_network_call(name))
                )

            watchdog.start()
            yield
    except AssertionError as observed_failure:
        # Testele de aici își țin aserțiile *după* blocul `with`, deci verificarea
        # de mai jos apucă să vorbească prima. Ramura aceasta acoperă testul
        # scris în cealaltă ordine, cu verificările înăuntru: acolo o buclă
        # oprită forțat s-ar vedea mai întâi ca listă prea scurtă
        # ("[1, 2] != [1, 2, 3]"), adică simptomul, iar cauza s-ar pierde. Cauza
        # trece în față, cu eșecul observat atașat drept cauză a ei.
        if watchdog.fired:
            raise AssertionError(watchdog.failure_message()) from observed_failure
        raise
    finally:
        watchdog.cancel()

    # Verificarea nu stă în `finally`: dacă testul a eșuat oricum din alt motiv,
    # acel eșec are prioritate și nu are voie să fie înlocuit de al nostru.
    if watchdog.fired:
        raise AssertionError(watchdog.failure_message())




class HeartbeatLoopSequenceTests(unittest.TestCase):
    """Secvența crește o dată per *încercare*, inclusiv peste eșecuri de rețea."""

    def test_loop_sends_incrementing_sequence_with_instance_id(self) -> None:
        config = _make_config()
        stop_event = threading.Event()
        sent_payloads: list = []

        def fake_send_heartbeat(server_url, agent_id, heartbeat_payload):
            sent_payloads.append(dict(heartbeat_payload))

            if len(sent_payloads) == 2:
                # Încercare eșuată: secvența trebuie să avanseze oricum, altfel
                # serverul nu ar putea deosebi o încercare pierdută de o
                # retransmisie a celei precedente (ramura de duplicat idempotent
                # din record_heartbeat). Contorul numără încercări, nu ferestre de
                # heartbeat — durata penei o măsoară serverul din last_seen.
                raise TransportError("server indisponibil")

            if len(sent_payloads) == 3:
                stop_event.set()

            return {"status": "ok", "directive": {"action": "none"}}

        with _loop_isolated_from_the_network(fake_send_heartbeat, stop_event):
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

        with _loop_isolated_from_the_network(fake_send_heartbeat, stop_event):
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

    def test_loop_keeps_its_cadence_when_the_server_sends_an_unusable_value(self) -> None:
        """
        Perechea testului de mai sus: ce *nu* are voie să devină cadență.

        Membrul interesant al listei este True. bool e subclasă de int, deci o
        verificare scrisă doar cu isinstance(..., (int, float)) îl acceptă, iar
        stop_event.wait(timeout=True) așteaptă o secundă — de zece ori cadența
        intenționată, simultan pe tot parcul de agenți. Restul valorilor sunt
        deja respinse azi; sunt aici ca să fixeze întregul contract al câmpului,
        nu doar cazul care a fost spart.

        nan e singura valoare numerică ce ar trece de o verificare de tip și ar
        rupe și comparația `next_interval != current_interval`, care în cazul lui
        este adevărată față de orice — inclusiv față de el însuși.
        """
        local_interval = 10

        for unusable in (True, False, 0, -5, "30", None, float("nan")):
            with self.subTest(next_heartbeat_seconds=unusable):
                stop_event = _RecordingStopEvent()

                def fake_send_heartbeat(server_url, agent_id, heartbeat_payload):
                    stop_event.set()
                    return {
                        "status": "ok",
                        "agent_id": agent_id,
                        DIRECTIVE_FIELD: {DIRECTIVE_ACTION_FIELD: "none"},
                        NEXT_INTERVAL_FIELD: unusable,
                    }

                with _loop_isolated_from_the_network(fake_send_heartbeat, stop_event):
                    agent.heartbeat_loop(
                        config=_make_config(),
                        server_url="http://127.0.0.1:8000",
                        system_info={},
                        heartbeat_interval_seconds=local_interval,
                        stop_event=stop_event,
                    )

                self.assertEqual(
                    stop_event.wait_timeouts,
                    [local_interval],
                    f"Agentul a adoptat {unusable!r} ca interval de heartbeat.",
                )


class HeartbeatLoopReregistrationTests(unittest.TestCase):
    """
    O re-înregistrare reușită este o recuperare, nu un eșec de heartbeat.

    Bucla află pe două căi distincte că serverul nu o mai cunoaște, iar ambele
    duc în același loc — register_agent_with_retry:

      - directiva "reregister" dintr-un răspuns HTTP 200 (agent.py, ramura
        `action == "reregister"`). Aceasta este singura cale pe care o produce
        serverul actual: la agent necunoscut răspunde 200 cu
        status="unregistered", ca să poată transporta și next_heartbeat_seconds.
      - AgentNotRegisteredError, ridicată de transport la HTTP 404.

    De ce testul le rulează pe amândouă (#11):
        A doua cale nu se execută niciodată contra serverului actual, deci se
        poate strica fără ca vreo rulare reală să o arate. Exact asta s-a
        întâmplat în #10: ramura de 404 contabiliza re-înregistrarea reușită
        drept eșec de heartbeat, iar bug-ul a supraviețuit tocmai pentru că
        nicio rulare nu ajungea acolo. Cât timp ramura rămâne plasă de siguranță
        pentru un server de altă versiune sau un proxy care întoarce 404,
        singurul lucru care o ține sincronizată cu calea activă este testul.
    """

    NORMAL_INTERVAL = 10
    FAILURES_BEFORE_RECOVERY = 3

    def _wait_after_reregistration(self, recovery) -> float:
        """
        Rulează bucla prin: eșecuri reale de rețea → recuperare → heartbeat bun.

        `recovery` decide *doar* cum află agentul că serverul nu îl mai
        recunoaște — aruncând AgentNotRegisteredError sau întorcând răspunsul cu
        directiva "reregister". Restul scenariului este identic pe ambele căi,
        deci orice diferență între rezultate vine din ramura pe care a intrat
        bucla, nu din montaj.

        Eșecurile de dinainte sunt esențiale: ele urcă seria de eșecuri
        consecutive, iar întrebarea testului este dacă recuperarea o resetează.
        Fără ele, ambele căi ar întoarce intervalul normal chiar și greșit
        implementate, pentru că nu ar exista nicio serie de resetat.

        Returnează pauza cerută imediat după re-înregistrarea reușită.
        """
        stop_event = _RecordingStopEvent()
        attempts = {"count": 0}
        recovery_wait = {}

        def fake_send_heartbeat(server_url, agent_id, heartbeat_payload):
            attempts["count"] += 1

            if attempts["count"] <= self.FAILURES_BEFORE_RECOVERY:
                # Server jos: eșecuri reale, care au voie să escaladeze.
                raise TransportError("connection refused")

            if attempts["count"] == self.FAILURES_BEFORE_RECOVERY + 1:
                # Server revenit, dar cu store gol: nu mai știe agentul.
                # Pauza de imediat după re-înregistrare este cea măsurată.
                recovery_wait["index"] = len(stop_event.wait_timeouts)
                return recovery(agent_id)

            stop_event.set()
            return {"status": "ok", DIRECTIVE_FIELD: {DIRECTIVE_ACTION_FIELD: "none"}}

        # register_agent_with_retry este înlocuit deliberat: harness-ul sigilează
        # check_server_health și register_agent, iar aici calea de re-înregistrare
        # este chiar subiectul testului, nu ceva de traversat până la rețea.
        with _loop_isolated_from_the_network(fake_send_heartbeat, stop_event), \
                patch.object(agent, "register_agent_with_retry", return_value=True):
            agent.heartbeat_loop(
                config=_make_config(),
                server_url="http://127.0.0.1:8000",
                system_info={},
                heartbeat_interval_seconds=self.NORMAL_INTERVAL,
                stop_event=stop_event,
            )

        return stop_event.wait_timeouts[recovery_wait["index"]]

    @staticmethod
    def _raises_not_registered(agent_id):
        """Calea de 404: transportul ridică excepția în locul unui răspuns."""
        raise AgentNotRegisteredError("404 agent not found")

    @classmethod
    def _answers_with_reregister_directive(cls, agent_id) -> dict:
        """
        Calea activă: răspunsul real al serverului la agent necunoscut.

        Reprodus după app/routes/heartbeat.py — HTTP 200, status "unregistered",
        directivă de reînregistrare și cadența dictată mai departe.
        """
        return {
            "status": "unregistered",
            "agent_id": agent_id,
            DIRECTIVE_FIELD: {DIRECTIVE_ACTION_FIELD: "reregister"},
            NEXT_INTERVAL_FIELD: cls.NORMAL_INTERVAL,
        }

    def test_the_404_path_clears_the_failure_streak(self) -> None:
        """
        Regresia pe care o prinde: backoff.record_failure() pe calea de succes.

        Seria de eșecuri de dinaintea lui 404 rămânea în contor, iar agentul
        tăcea un multiplu al intervalului normal deși conexiunea era restabilită.
        """
        self.assertEqual(
            self._wait_after_reregistration(self._raises_not_registered),
            self.NORMAL_INTERVAL,
            "După o re-înregistrare reușită agentul a așteptat un backoff de "
            "eșec în loc de intervalul normal: seria de eșecuri nu a fost "
            "resetată, deși serverul tocmai răspunsese.",
        )

    def test_the_directive_path_clears_the_failure_streak(self) -> None:
        """Calea produsă de serverul actual, verificată cu aceeași măsură."""
        self.assertEqual(
            self._wait_after_reregistration(self._answers_with_reregister_directive),
            self.NORMAL_INTERVAL,
            "Directiva de reînregistrare a fost onorată, dar agentul a rămas pe "
            "un backoff de eșec în loc de cadența normală.",
        )

    def test_both_reregistration_paths_agree(self) -> None:
        """
        Divergența dintre cele două căi este ea însăși defectul (#11).

        Testele de mai sus fixează *valoarea* pauzei; acesta fixează *acordul*.
        Distincția contează la o schimbare deliberată de comportament: dacă
        cineva decide că după o re-înregistrare se așteaptă altceva decât
        intervalul normal, celelalte două trebuie actualizate, dar acesta
        trebuie să continue să treacă — altfel schimbarea a fost aplicată pe o
        singură cale, iar cea care nu se execută niciodată a rămas în urmă.
        """
        self.assertEqual(
            self._wait_after_reregistration(self._raises_not_registered),
            self._wait_after_reregistration(self._answers_with_reregister_directive),
            "Cele două căi de re-înregistrare au ajuns la pauze diferite. Una "
            "dintre ele nu se execută niciodată contra serverului actual, deci "
            "diferența nu ar apărea în nicio rulare reală.",
        )


if __name__ == "__main__":
    unittest.main()
