"""
Teste pentru contabilizarea octetilor in stratul de transport (1.3b.1 si 1.3b.3).
=================================================================================

test_wire_ledger.py apara registrul ca obiect. Aici se apara CABLAREA lui: ca
fiecare cerere chiar trece prin contoare, pe canalul corect, in cele doua
momente corecte — si ca cifrele pleaca spre server in anteturi, decalate cu un
mesaj.

Cel mai important test din fisier e cel cu 401. Pozitia lui record_delivery in
post_request — inainte de handle_response, nu dupa — e o linie usor de mutat
inapoi de cineva care ordoneaza codul dupa estetica. Daca se muta, fiecare refuz
al serverului devine o nelivrare, iar un agent cu cheia gresita care trimite ore
in sir apare ca si cum n-ar divulga nimic. Testul acela e singurul lucru care
sta intre cifra publicata si eroarea aceea.
"""

import pathlib
import unittest
from unittest.mock import patch

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout

import services.transport as transport
from services.transport import (
    WIRE_ATTEMPTED_HEADER,
    WIRE_DELIVERED_HEADER,
    WIRE_INSTANCE_HEADER,
    AuthenticationError,
    FatalTransportError,
    TransportError,
    post_request,
    register_agent,
    send_event,
    send_heartbeat,
    serialize_payload,
)
from services.wire_ledger import (
    CHANNEL_CONTROL,
    CHANNEL_ENROLLMENT,
    CHANNEL_EVENTS,
    WireLedger,
    get_wire_ledger,
)
from tests.test_wire_serialization import PAYLOAD, RecordingSession, as_session


BODY_SIZE = len(serialize_payload(PAYLOAD))


class FailingSession:
    """Sesiune care moare inainte sa vina vreun raspuns."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def post(self, *args, **kwargs):
        raise self._error


class GettableSession:
    """Sesiune falsa pentru ruta de GET, singura care nu are corp."""

    def get(self, *args, **kwargs):
        return _HealthResponse()


class _HealthResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"status": "ok"}


class HappyPathTests(unittest.TestCase):
    def test_a_successful_post_counts_the_exact_serialized_size(self) -> None:
        ledger = WireLedger()

        post_request(
            "http://server",
            "/api/events",
            PAYLOAD,
            channel=CHANNEL_EVENTS,
            session=as_session(RecordingSession()),
            ledger=ledger,
        )

        counters = ledger.snapshot().channels[CHANNEL_EVENTS]
        self.assertEqual(counters.attempted_bytes, BODY_SIZE)
        self.assertEqual(counters.delivered_bytes, BODY_SIZE)
        self.assertEqual(counters.attempted_messages, 1)
        self.assertEqual(counters.delivered_messages, 1)

    def test_the_counted_number_is_the_body_that_actually_went_out(self) -> None:
        # Nu o reestimare a payload-ului, ci exact octetii pasati lui requests.
        # Daca cele doua ar diverge vreodata, reconcilierea de pe server ar
        # acuza un canal necontabilizat acolo unde nu e decat o alta serializare.
        session = RecordingSession()
        ledger = WireLedger()

        post_request(
            "http://server",
            "/api/events",
            PAYLOAD,
            channel=CHANNEL_EVENTS,
            session=as_session(session),
            ledger=ledger,
        )

        sent_body = session.calls[0]["data"]
        self.assertEqual(
            ledger.snapshot().totals.attempted_bytes, len(sent_body)
        )


class ARefusalIsStillADeliveryTests(unittest.TestCase):
    """
    Ca sa te poata refuza, serverul trebuie mai intai sa fi primit mesajul.
    """

    def _post_expecting(self, status_code: int, expected_error):
        ledger = WireLedger()
        session = RecordingSession(status_code=status_code, json_body={"detail": "nu"})

        with self.assertRaises(expected_error):
            post_request(
                "http://server",
                "/api/events",
                PAYLOAD,
                channel=CHANNEL_EVENTS,
                session=as_session(session),
                ledger=ledger,
            )

        return ledger.snapshot().channels[CHANNEL_EVENTS]

    def test_a_401_still_counts_as_delivered(self) -> None:
        # Scenariul care conteaza: agent cu cheia gresita, care trimite ore in
        # sir. Divulga tot ce trimite, chiar daca nimic nu e acceptat.
        counters = self._post_expecting(401, AuthenticationError)

        self.assertEqual(counters.attempted_bytes, BODY_SIZE)
        self.assertEqual(counters.delivered_bytes, BODY_SIZE)
        self.assertEqual(counters.undelivered_bytes, 0)

    def test_a_422_still_counts_as_delivered(self) -> None:
        # Payload refuzat de schema serverului. Mesajul e stricat, dar a ajuns.
        counters = self._post_expecting(422, FatalTransportError)

        self.assertEqual(counters.delivered_bytes, BODY_SIZE)

    def test_a_500_still_counts_as_delivered(self) -> None:
        # Serverul a primit si a cazut incercand sa proceseze. Tot livrare e.
        counters = self._post_expecting(500, TransportError)

        self.assertEqual(counters.delivered_bytes, BODY_SIZE)


class WhatLeftIntoNothingTests(unittest.TestCase):
    """Octetii plecati fara raspuns raman contati ca plecati."""

    def _post_failing_with(self, error: Exception):
        ledger = WireLedger()

        with self.assertRaises(TransportError):
            post_request(
                "http://server",
                "/api/events",
                PAYLOAD,
                channel=CHANNEL_EVENTS,
                session=as_session(FailingSession(error)),
                ledger=ledger,
            )

        return ledger.snapshot().channels[CHANNEL_EVENTS]

    def test_a_timeout_leaves_the_bytes_counted_as_departed(self) -> None:
        counters = self._post_failing_with(Timeout("prea tarziu"))

        self.assertEqual(counters.attempted_bytes, BODY_SIZE)
        self.assertEqual(counters.delivered_bytes, 0)
        self.assertEqual(counters.undelivered_bytes, BODY_SIZE)

    def test_a_dead_connection_leaves_the_bytes_counted_as_departed(self) -> None:
        counters = self._post_failing_with(RequestsConnectionError("nimeni acasa"))

        self.assertEqual(counters.attempted_bytes, BODY_SIZE)
        self.assertEqual(counters.delivered_bytes, 0)

    def test_a_payload_that_never_serialized_never_left(self) -> None:
        # Aici nu e vorba de un mesaj pierdut, ci de unul care n-a existat
        # niciodata ca octeti. Nu are ce sa numere.
        ledger = WireLedger()
        session = RecordingSession()

        with self.assertRaises(TransportError):
            post_request(
                "http://server",
                "/api/events",
                {"nan": float("nan")},
                channel=CHANNEL_EVENTS,
                session=as_session(session),
                ledger=ledger,
            )

        self.assertEqual(ledger.snapshot().totals.attempted_bytes, 0)
        self.assertEqual(ledger.snapshot().totals.attempted_messages, 0)


class ChannelRoutingTests(unittest.TestCase):
    """
    Fiecare functie de deasupra isi declara canalul, o singura data.

    Testele patch-uiesc post_request si se uita la argumentul primit: ce se
    verifica aici e atribuirea, nu numaratoarea. Daca send_heartbeat ar trimite
    pe canalul de evenimente, heartbeat-urile s-ar topi in divulgare, iar
    podeaua din par. 1.4 ar disparea din telemetrie fara ca vreo cifra sa para
    gresita.
    """

    def _channel_used_by(self, call) -> str:
        recorded = {}

        def fake_post_request(server_url, endpoint, payload, **kwargs):
            recorded.update(kwargs)
            return {"status": "ok"}

        with patch.object(transport, "post_request", fake_post_request):
            call()

        return recorded["channel"]

    def test_events_go_on_the_events_channel(self) -> None:
        self.assertEqual(
            self._channel_used_by(
                lambda: send_event("http://server", PAYLOAD, agent_key="k")
            ),
            CHANNEL_EVENTS,
        )

    def test_heartbeats_go_on_the_control_channel(self) -> None:
        self.assertEqual(
            self._channel_used_by(
                lambda: send_heartbeat("http://server", "agent-1", agent_key="k")
            ),
            CHANNEL_CONTROL,
        )

    def test_registration_forwards_the_incarnation_to_the_header(self) -> None:
        # register_agent() e singura functie care primeste incarnarea ca
        # parametru, pentru ca e singura al carei corp n-are voie s-o poarte.
        recorded = {}

        def fake_post_request(server_url, endpoint, payload, **kwargs):
            recorded.update(kwargs)
            return {"status": "ok"}

        with patch.object(transport, "post_request", fake_post_request):
            register_agent(
                "http://server",
                {"agent_id": "agent-1"},
                agent_key="k",
                agent_instance_id="incarnare-3",
            )

        self.assertEqual(recorded["agent_instance_id"], "incarnare-3")

    def test_registration_goes_on_the_enrollment_channel(self) -> None:
        self.assertEqual(
            self._channel_used_by(
                lambda: register_agent("http://server", PAYLOAD, agent_key="k")
            ),
            CHANNEL_ENROLLMENT,
        )


class DefaultLedgerTests(unittest.TestCase):
    def test_the_module_ledger_is_one_object_for_the_whole_incarnation(self) -> None:
        self.assertIs(get_wire_ledger(), get_wire_ledger())

    def test_a_post_without_an_injected_ledger_lands_in_the_module_one(self) -> None:
        # Singurul test care atinge registrul global, si o face aditiv: masoara
        # diferenta, nu valoarea absoluta, ca sa nu depinda de ordinea in care
        # ruleaza suita.
        before = get_wire_ledger().snapshot().channels[CHANNEL_EVENTS]

        post_request(
            "http://server",
            "/api/events",
            PAYLOAD,
            channel=CHANNEL_EVENTS,
            session=as_session(RecordingSession()),
        )

        after = get_wire_ledger().snapshot().channels[CHANNEL_EVENTS]
        self.assertEqual(after.attempted_bytes - before.attempted_bytes, BODY_SIZE)
        self.assertEqual(after.delivered_bytes - before.delivered_bytes, BODY_SIZE)


class WireHeadersTests(unittest.TestCase):
    """
    Anteturile poarta totalul de DINAINTEA cererii curente.

    Decalajul cu un mesaj nu e o aproximare, e singurul mod de a rupe
    circularitatea: ca sa declare dimensiunea cererii curente, agentul ar trebui
    sa puna acea dimensiune intr-un antet al aceleiasi cereri, care ii schimba
    dimensiunea.
    """

    def _headers_of_each_call(self, count: int, ledger: WireLedger):
        session = RecordingSession()

        for _ in range(count):
            post_request(
                "http://server",
                "/api/events",
                PAYLOAD,
                channel=CHANNEL_EVENTS,
                session=as_session(session),
                ledger=ledger,
            )

        return [call["headers"] for call in session.calls]

    def test_the_first_request_of_an_incarnation_declares_two_zeros(self) -> None:
        # Zero afirmat, nu antet lipsa. Un antet absent nu se distinge de un
        # agent vechi, de un intermediar care taie anteturi, sau de un bug.
        headers = self._headers_of_each_call(1, WireLedger())[0]

        self.assertEqual(headers[WIRE_ATTEMPTED_HEADER], "0")
        self.assertEqual(headers[WIRE_DELIVERED_HEADER], "0")

    def test_each_request_reports_everything_except_itself(self) -> None:
        headers = self._headers_of_each_call(3, WireLedger())

        self.assertEqual(headers[0][WIRE_ATTEMPTED_HEADER], "0")
        self.assertEqual(headers[1][WIRE_ATTEMPTED_HEADER], str(BODY_SIZE))
        self.assertEqual(headers[2][WIRE_ATTEMPTED_HEADER], str(2 * BODY_SIZE))

    def test_delivered_lags_attempted_by_the_message_in_flight(self) -> None:
        # La cererea N, mesajul N-1 e deja incheiat, deci ambele contoare il
        # includ. Cifrele coincid pentru ca sesiunea falsa raspunde mereu; ce
        # verificam aici e ca nu se strecoara cererea curenta in niciunul.
        headers = self._headers_of_each_call(2, WireLedger())

        self.assertEqual(headers[1][WIRE_DELIVERED_HEADER], str(BODY_SIZE))
        self.assertEqual(headers[1][WIRE_ATTEMPTED_HEADER], str(BODY_SIZE))

    def test_a_lost_message_shows_up_as_a_gap_between_the_two_headers(self) -> None:
        # Cazul pentru care exista doua anteturi in loc de unul: un mesaj plecat
        # fara raspuns ramane in attempted si lipseste din delivered. Serverul
        # citeste diferenta fara sa fi vazut vreodata mesajul pierdut.
        ledger = WireLedger()

        with self.assertRaises(TransportError):
            post_request(
                "http://server",
                "/api/events",
                PAYLOAD,
                channel=CHANNEL_EVENTS,
                session=as_session(FailingSession(Timeout("prea tarziu"))),
                ledger=ledger,
            )

        headers = self._headers_of_each_call(1, ledger)[0]
        self.assertEqual(headers[WIRE_ATTEMPTED_HEADER], str(BODY_SIZE))
        self.assertEqual(headers[WIRE_DELIVERED_HEADER], "0")

    def test_the_totals_span_all_channels(self) -> None:
        # Serverul nu vede canalele noastre, deci compara un singur numar.
        ledger = WireLedger()
        ledger.record_attempt(CHANNEL_CONTROL, 41)
        ledger.record_delivery(CHANNEL_CONTROL, 41)

        headers = self._headers_of_each_call(1, ledger)[0]

        self.assertEqual(headers[WIRE_ATTEMPTED_HEADER], "41")
        self.assertEqual(headers[WIRE_DELIVERED_HEADER], "41")

    def test_the_incarnation_travels_as_a_header_too(self) -> None:
        # Serverul contabilizeaza in middleware, inainte de rutare, deci nu
        # poate citi corpul: ar consuma fluxul asteptat de ruta. Antetul e
        # singurul loc din care poate afla cui apartin cifrele raportate.
        session = RecordingSession()

        post_request(
            "http://server",
            "/api/events",
            dict(PAYLOAD, agent_instance_id="incarnare-7"),
            channel=CHANNEL_EVENTS,
            session=as_session(session),
            ledger=WireLedger(),
        )

        self.assertEqual(session.calls[0]["headers"][WIRE_INSTANCE_HEADER], "incarnare-7")

    def test_a_payload_without_an_incarnation_omits_the_header(self) -> None:
        # Cazul real e inregistrarea, singurul payload fara agent_instance_id.
        # Un antet gol ar arata ca o valoare prezenta si ar strica o cheie de
        # contabilizare pe server; unul absent spune adevarul, iar serverul are
        # pentru asta o galeata de octeti neatribuibili.
        session = RecordingSession()

        post_request(
            "http://server",
            "/api/agents/register",
            {"agent_id": "agent-1", "hostname": "masina"},
            channel=CHANNEL_ENROLLMENT,
            session=as_session(session),
            ledger=WireLedger(),
        )

        self.assertNotIn(WIRE_INSTANCE_HEADER, session.calls[0]["headers"])

    def test_the_incarnation_can_come_from_outside_the_payload(self) -> None:
        # Cazul inregistrarii. Contractul interzice agent_instance_id in CORPUL
        # inregistrarii, pentru ca ar suprascrie baseline-ul de repornire
        # inainte ca heartbeat-ul sa-l compare. Interdictia e pe corp, nu pe
        # cerere: antetul nu trece prin schema serverului si nu ajunge in
        # agents_store.
        session = RecordingSession()
        body = {"agent_id": "agent-1", "hostname": "masina"}

        post_request(
            "http://server",
            "/api/agents/register",
            body,
            channel=CHANNEL_ENROLLMENT,
            session=as_session(session),
            ledger=WireLedger(),
            agent_instance_id="incarnare-9",
        )

        call = session.calls[0]
        assert call["headers"][WIRE_INSTANCE_HEADER] == "incarnare-9"
        # Garda care conteaza: valoarea data pe langa payload nu are voie sa
        # ajunga in corp. Daca ar ajunge, detectia de repornire moare tacut.
        self.assertNotIn(b"agent_instance_id", call["data"])

    def test_an_explicit_incarnation_wins_over_the_payload(self) -> None:
        # Nu se intampla in practica — cele doua surse nu coexista — dar regula
        # trebuie sa fie scrisa undeva, altfel urmatorul cititor o ghiceste.
        session = RecordingSession()

        post_request(
            "http://server",
            "/api/events",
            dict(PAYLOAD, agent_instance_id="din-corp"),
            channel=CHANNEL_EVENTS,
            session=as_session(session),
            ledger=WireLedger(),
            agent_instance_id="explicita",
        )

        self.assertEqual(
            session.calls[0]["headers"][WIRE_INSTANCE_HEADER], "explicita"
        )

    def test_an_empty_incarnation_is_treated_as_absent(self) -> None:
        session = RecordingSession()

        post_request(
            "http://server",
            "/api/events",
            dict(PAYLOAD, agent_instance_id=""),
            channel=CHANNEL_EVENTS,
            session=as_session(session),
            ledger=WireLedger(),
        )

        self.assertNotIn(WIRE_INSTANCE_HEADER, session.calls[0]["headers"])

    def test_the_headers_ride_along_with_authentication(self) -> None:
        session = RecordingSession()

        post_request(
            "http://server",
            "/api/events",
            PAYLOAD,
            channel=CHANNEL_EVENTS,
            session=as_session(session),
            headers={"X-Agent-Key": "cheie"},
            ledger=WireLedger(),
        )

        headers = session.calls[0]["headers"]
        self.assertEqual(headers["X-Agent-Key"], "cheie")
        self.assertIn(WIRE_ATTEMPTED_HEADER, headers)
        self.assertIn(WIRE_DELIVERED_HEADER, headers)


class HeadersAreDocumentedTests(unittest.TestCase):
    """
    Numele anteturilor din cod trebuie sa apara in contract, verbatim.

    E acelasi rationament care a nascut tests/wire_contract.py: un nume copiat
    de mana in doua locuri se desincronizeaza tacut. Aici copia e intre
    services/transport.py si contracts/METRICS.md, iar consecinta unei
    redenumiri nedocumentate e ca serverul, scris dupa contract, cauta un antet
    care nu mai vine — si vede zero divulgare raportata, nu o eroare.

    Nu inlocuieste sincronizarea cu edr-server, care se face pe fisierul de
    contract; verifica doar ca fisierul acela spune ce face codul.
    """

    def _metrics_contract(self) -> str:
        contract = (
            pathlib.Path(__file__).resolve().parents[1] / "contracts" / "METRICS.md"
        )
        # utf-8-sig, ca in wire_contract.py: editoarele Windows salveaza des cu BOM.
        return contract.read_text(encoding="utf-8-sig")

    def test_both_header_names_appear_in_the_metrics_contract(self) -> None:
        document = self._metrics_contract()

        self.assertIn(WIRE_ATTEMPTED_HEADER, document)
        self.assertIn(WIRE_DELIVERED_HEADER, document)
        self.assertIn(WIRE_INSTANCE_HEADER, document)


class UncountedByDesignTests(unittest.TestCase):
    def test_a_get_request_is_not_counted(self) -> None:
        # GET /health nu are corp, iar par. 1.2 exclude explicit anteturile HTTP
        # din divulgare: sunt cost de transport, identice sub ambele politici,
        # deci s-ar aduna la fel in numarator si in numitor. Ce ramane de numarat
        # e zero, deci get_request nu trece deloc prin registru.
        #
        # Testul chiar executa get_request, cu o sesiune falsa: daca ar patch-ui
        # get_request, ar scoate din joc exact lucrul care ar fi putut numara si
        # n-ar mai verifica nimic.
        before = get_wire_ledger().snapshot().totals

        result = transport.get_request(
            "http://server", "/health", session=as_session(GettableSession())
        )

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(get_wire_ledger().snapshot().totals, before)


if __name__ == "__main__":
    unittest.main()
