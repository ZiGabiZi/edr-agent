"""
Teste pentru serializarea explicita a corpurilor POST (pasul 1.3b.2).
======================================================================

De ce exista fisierul asta, cand nimic nu se comporta altfel dupa schimbare:

    Pana la comitul care l-a adus, agentul pasa `json=payload` catre `requests`,
    iar biblioteca serializa singura, dupa ce agentul predase obiectul. Agentul
    nu vedea niciodata octetii pe care ii trimite, deci nu putea numara decat
    reserializand mesajul a doua oara — o estimare, nu o masuratoare. METRICS.md
    §7 cere ca numaratorul afirmatiei centrale sa fie masurat, deci serializarea
    trebuia sa se mute in agent.

    Schimbarea atinge FIECARE cerere pe care agentul o face — evenimente,
    heartbeat, inregistrare — si nu are voie sa mute nimic observabil. Testele
    de aici sunt exact dovada aceea, iar cea mai importanta e prima: compara
    octetii nostri cu octetii pe care i-ar fi produs `requests`, prin propriul
    lui mecanism de pregatire a cererii. Daca cineva compacteaza separatorii sau
    schimba `ensure_ascii`, cifra de divulgare se muta — iar aici pica un test,
    nu se descopera peste trei luni intr-o reconciliere care nu iese.
"""

import json
import unittest
from typing import Any, Dict, List, cast

import requests
from requests import Session

from services.transport import (
    JSON_CONTENT_TYPE,
    FatalTransportError,
    TransportError,
    build_json_headers,
    post_request,
    serialize_payload,
)


class RecordingSession:
    """Sesiune falsa care retine exact ce a primit `post`."""

    def __init__(self, status_code: int = 200, json_body=None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._status_code = status_code
        self._json_body = {"status": "ok"} if json_body is None else json_body

    def post(self, url, data=None, timeout=None, headers=None, **kwargs):
        self.calls.append(
            {
                "url": url,
                "data": data,
                "timeout": timeout,
                "headers": headers,
                "extra": kwargs,
            }
        )
        return _FakeResponse(self._status_code, self._json_body)


class _FakeResponse:
    def __init__(self, status_code, json_body) -> None:
        self.status_code = status_code
        self.text = json.dumps(json_body)
        self._json_body = json_body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


def as_session(recorder: RecordingSession) -> Session:
    """
    Prezinta sesiunea falsa la semnatura lui post_request().

    Nu e o cosmetizare de verificator de tipuri, e o inexactitate reala pe care
    o marcam intr-un singur loc. post_request() declara `Optional[Session]`, dar
    nu foloseste nimic din Session: face `client = session or requests` si apoi
    `client.post(...)`. Adica accepta deja modulul `requests`, care nu e nici el
    o Session — semnatura cere mai mult decat are nevoie codul.

    Anotarea corecta ar fi un Protocol cu un singur `post`, in transport.py,
    aplicat si lui get_request. Aceea e o curatenie separata, de facut deliberat;
    pana atunci, minciuna sta aici, explicita, si nu imprastiata prin teste.
    """
    return cast(Session, recorder)


PAYLOAD = {
    "agent_id": "agent-test",
    "event_type": "file_created",
    "file_path": "C:/tmp/proba.exe",
    "disclosure": {"tier": "T1", "content_bytes": 0},
}


class SerializationIsByteIdenticalTests(unittest.TestCase):
    """Serializarea noastra produce exact ce producea `requests`."""

    def _requests_body(self, payload) -> bytes:
        """Octetii pe care i-ar fi pus pe fir `requests` cu `json=payload`."""
        prepared = requests.Request(
            method="POST", url="http://server/api/events", json=payload
        ).prepare()
        # PreparedRequest.body e declarat larg (poate fi text, bytes sau flux,
        # dupa cum a fost construita cererea). Pe ramura `json=` biblioteca
        # incheie mereu cu .encode("utf-8"), deci aici sunt octeti — verificam
        # asta in loc s-o presupunem, ca sa nu compare testul text cu octeti
        # daca ramura se schimba vreodata.
        assert isinstance(prepared.body, bytes)
        return prepared.body

    def test_our_bytes_match_what_requests_would_have_sent(self) -> None:
        self.assertEqual(serialize_payload(PAYLOAD), self._requests_body(PAYLOAD))

    def test_the_match_holds_for_non_ascii_paths(self) -> None:
        # Cazul care rupe cel mai discret: o cale cu diacritice. Cu
        # ensure_ascii=True fiecare devine o secventa escapata, mai lunga in
        # octeti decat pare in caractere — si e corect asa, octetii aceia chiar
        # pleaca de pe endpoint. Ce conteaza e sa fie aceiasi ca inainte.
        payload = dict(PAYLOAD, file_path="C:/tmp/raport final însemnări.exe")
        self.assertEqual(serialize_payload(payload), self._requests_body(payload))

    def test_serialization_returns_bytes_not_text(self) -> None:
        # `len()` pe un str numara caractere, pe bytes numara octeti. Divulgarea
        # se masoara in octeti, deci tipul e parte din definitie, nu detaliu.
        self.assertIsInstance(serialize_payload(PAYLOAD), bytes)

    def test_length_of_the_body_is_the_number_the_ledger_will_count(self) -> None:
        body = serialize_payload(PAYLOAD)
        self.assertEqual(len(body), len(json.dumps(PAYLOAD).encode("utf-8")))


class JsonHeaderTests(unittest.TestCase):
    """Antetul pe care nu-l mai pune `requests` de la sine."""

    def test_content_type_is_added_when_missing(self) -> None:
        self.assertEqual(
            build_json_headers({"X-Agent-Key": "k"}),
            {"X-Agent-Key": "k", "Content-Type": JSON_CONTENT_TYPE},
        )

    def test_content_type_is_added_when_there_are_no_headers_at_all(self) -> None:
        self.assertEqual(build_json_headers(None), {"Content-Type": JSON_CONTENT_TYPE})

    def test_a_caller_supplied_content_type_is_not_overwritten(self) -> None:
        # Acelasi comportament pe care il avea `requests`: antetul pus de
        # apelant castiga, iar comparatia e insensibila la majuscule.
        headers = {"content-type": "application/json; charset=utf-8"}
        self.assertEqual(build_json_headers(headers), headers)

    def test_the_caller_dictionary_is_not_mutated(self) -> None:
        # Apelantii construiesc antetele cu build_auth_headers() si le refolosesc;
        # un dictionar care creste pe la spate e o clasa de bug greu de vazut.
        headers = {"X-Agent-Key": "k"}
        build_json_headers(headers)
        self.assertEqual(headers, {"X-Agent-Key": "k"})


class PostRequestSendsPreSerializedBodyTests(unittest.TestCase):
    def test_body_goes_out_as_data_not_as_json(self) -> None:
        session = RecordingSession()

        post_request(
            "http://server", "/api/events", PAYLOAD, session=as_session(session)
        )

        call = session.calls[0]
        self.assertEqual(call["data"], serialize_payload(PAYLOAD))
        # Daca `json=` ar mai fi pasat, ar aparea aici si am serializa de doua ori.
        self.assertNotIn("json", call["extra"])

    def test_auth_headers_survive_alongside_the_content_type(self) -> None:
        session = RecordingSession()

        post_request(
            "http://server",
            "/api/events",
            PAYLOAD,
            session=as_session(session),
            headers={"X-Agent-Key": "cheie"},
        )

        self.assertEqual(
            session.calls[0]["headers"],
            {"X-Agent-Key": "cheie", "Content-Type": JSON_CONTENT_TYPE},
        )

    def test_an_unserializable_payload_stays_a_temporary_error(self) -> None:
        # Clasificarea de dinainte: `requests` ridica InvalidJSONError, adica
        # RequestException, iar post_request o transforma in TransportError.
        # Nu FatalTransportError — aceea ar sterge evenimentul din spool, si
        # comitul de serializare nu are voie sa decida asa ceva.
        session = RecordingSession()

        with self.assertRaises(TransportError) as raised:
            post_request(
                "http://server",
                "/api/events",
                {"nan": float("nan")},
                session=as_session(session),
            )

        self.assertNotIsInstance(raised.exception, FatalTransportError)
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
