from typing import Any, Dict, Optional
from urllib.parse import quote

import requests
from requests import Response, Session
from requests.exceptions import RequestException, Timeout, ConnectionError as RequestsConnectionError


DEFAULT_TIMEOUT_SECONDS = 5

# Antetele de autentificare. Numele trebuie să corespundă exact celor din
# edr-server/app/services/auth_service.py — o literă greșită aici nu produce o
# eroare de import, ci un 401 permanent care arată ca o cheie greșită.
AGENT_KEY_HEADER = "X-Agent-Key"
ENROLLMENT_SECRET_HEADER = "X-Enrollment-Secret"


class TransportError(Exception):
    """Eroare ridicată atunci când comunicarea cu serverul EDR eșuează."""
    pass

class FatalTransportError(TransportError):
    """
    Eroare irecuperabilă: cererea nu va fi acceptată niciodată în forma asta.
    Practic, azi înseamnă 422 — un payload pe care schema serverului îl refuză.

    Criteriul e „proprietate a mesajului", nu „cod 4xx". Un payload stricat nu
    devine valid dacă îl mai trimiți o dată, deci păstrarea lui în coadă ar
    bloca la nesfârșit tot ce vine după el.

    401, 403 și 409 NU mai intră aici, deși sunt 4xx. Primele două descriu
    relația agent-server, al treilea descrie o coliziune de nume; niciunul nu
    descrie mesajul, deci niciunul nu justifică aruncarea lui.
    """
    pass


class AuthenticationError(TransportError):
    """
    Serverul nu recunoaște identitatea agentului (HTTP 401).

    Stare TEMPORARĂ, tratată ca atare: evenimentele rămân în coadă și se
    reîncearcă. Motivul e că 401 nu descrie mesajul, ci relația dintre agent și
    server la momentul cererii — și poate fi adevărat acum și fals peste zece
    minute, din cauze care n-au nimic de-a face cu evenimentul:

        - cheia a fost rotită, iar fișierul local n-a apucat să se actualizeze;
        - serverul a repornit cu un depozit de chei nou sau corupt;
        - o greșeală de deploy sau de tastare, reparată în câteva minute.

    De ce contează clasificarea, concret: până la comitul care a introdus
    excepția asta, 401 era FatalTransportError, iar EventDispatcher trata orice
    eroare fatală ca poison message și ștergea evenimentul din spool. Cu
    autentificarea pornită, cinci minute de cheie greșită ar fi șters ireversibil
    tot ce era în coadă — spool-ul persistent, construit ca să garanteze
    at-least-once, ar fi devenit at-most-once exact când ceva era stricat.

    Nici tratarea ca AgentNotRegisteredError nu era corectă: acolo recuperarea e
    automată (heartbeat-ul re-înregistrează). O cheie revocată intenționat nu se
    repară singură niciodată, deci tăcerea ar fi la fel de greșită ca ștergerea.
    De aceea recuperarea e „păstrează și escaladează în timp" —
    services/auth_alarm.py.
    """
    pass


class IdentityMismatchError(TransportError):
    """
    Identitate acceptată, acțiune refuzată (HTTP 403).

    Serverul recunoaște cheia, dar ea nu aparține agent_id-ului din cerere.
    Cazul tipic e fișierul de cheie al unei mașini copiat pe alta — o clonă de
    imagine, o restaurare greșită, un deploy manual.

    Nu se repară de la sine, dar tot NU e o proprietate a evenimentului:
    evenimentele sunt reale și aparțin mașinii care le-a produs; doar
    credențiala e greșită. Se păstrează în coadă și se raportează la nivel de
    ERROR de la prima apariție, pentru că spre deosebire de 401 aici nu există
    scenariu în care așteptarea rezolvă singură problema.
    """
    pass


class IdentityConflictError(TransportError):
    """
    agent_id-ul cerut e deja legat de altă mașină (HTTP 409).

    Tipic: o mașină clonată după înrolare, care duce cu ea fișierul de
    identitate și cheia, dar are altă amprentă de mașină.

    De ce NU e FatalTransportError, deși nu se repară de la sine: eroarea fatală
    face register_agent_with_retry să întoarcă False, iar run_agent sare atunci
    peste bucla de heartbeat și intră direct în finally — oprind file monitor-ul,
    dispatcher-ul și spool-ul. Adică un conflict de NUME oprea colectarea de
    evenimente pe o mașină perfect sănătoasă, iar aceasta dispărea complet din
    consolă: nici măcar ca agent cu probleme.

    Pentru un EDR, tăcerea totală e cel mai prost simptom posibil — nu se
    distinge de un endpoint oprit sau de unul compromis. Tratamentul e cel
    aplicat deja la 401 și 403: rămâi pornit, colectează local, escaladează în
    jurnal. Conflictul se repară de un om, iar când se repară, coada pleacă
    singură.
    """
    pass


class AgentNotRegisteredError(TransportError):
    """
    Serverul nu recunoaște agentul (HTTP 404 — agent inexistent în store,
    tipic după un restart al serverului cu store volatil).

    Spre deosebire de FatalTransportError, această stare este RECUPERABILĂ
    automat: bucla de heartbeat va re-înregistra agentul, după care
    trimiterea evenimentelor poate fi reluată. Nu justifică oprirea
    agentului și nici abandonarea evenimentelor.
    """
    pass


def build_url(server_url: str, endpoint: str) -> str:
    """
    Construiește URL-ul complet pentru o rută API.

    Exemplu:
    server_url = "http://192.168.16.10:8000"
    endpoint = "/health"
    rezultat = "http://192.168.16.10:8000/health"
    """
    return f"{server_url.rstrip('/')}/{endpoint.lstrip('/')}"


def parse_json_response(response: Response) -> Dict[str, Any]:
    """
    Parsează răspunsul JSON primit de la server.

    Dacă serverul nu întoarce JSON valid, se ridică o eroare controlată.
    """
    try:
        return response.json()
    except ValueError as error:
        raise TransportError("Server returned a non-JSON response") from error


def build_auth_headers(
    agent_key: Optional[str] = None,
    enrollment_secret: Optional[str] = None,
) -> Dict[str, str]:
    """
    Construiește antetele de autentificare pentru o cerere.

    Ambele pot fi prezente simultan, deliberat: la o reînrolare (cheia de pe
    disc s-a pierdut, dar secretul de înrolare a fost reprovizionat) serverul
    verifică întâi secretul, deci prezența cheii vechi nu blochează emiterea
    uneia noi. Vezi authorize_registration() pe server pentru ordine.
    """
    headers: Dict[str, str] = {}

    if agent_key:
        headers[AGENT_KEY_HEADER] = agent_key

    if enrollment_secret:
        headers[ENROLLMENT_SECRET_HEADER] = enrollment_secret

    return headers


def handle_response(response: Response) -> Dict[str, Any]:
    """
    Verifică status code-ul HTTP și întoarce răspunsul JSON.

    Clasificarea erorilor e împărțită pe cauză, nu pe familie de coduri, pentru
    că apelantul face lucruri ireversibile pe baza ei (EventDispatcher șterge
    evenimente la eroare fatală):

        401 -> AuthenticationError     — identitate nerecunoscută, recuperabilă
        403 -> IdentityMismatchError   — identitate acceptată, acțiune refuzată
        404 -> AgentNotRegisteredError — agent pierdut de server, recuperabil
        409 -> IdentityConflictError   — numele e al altei mașini
        alt 4xx (fără 408/429) -> FatalTransportError — mesajul e stricat
        restul -> TransportError       — temporar (rețea, 5xx, throttling)
    """
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        try:
            error_detail = response.json()
        except ValueError:
            error_detail = response.text

        if response.status_code == 401:
            raise AuthenticationError(
                f"Agent identity not recognized by server (HTTP 401): {error_detail}"
            ) from error

        if response.status_code == 403:
            raise IdentityMismatchError(
                f"Agent key does not authorize this agent_id (HTTP 403): {error_detail}"
            ) from error

        if response.status_code == 409:
            raise IdentityConflictError(
                f"agent_id already claimed by another machine (HTTP 409): {error_detail}"
            ) from error

        if response.status_code == 404:
            raise AgentNotRegisteredError(
                f"Agent not recognized by server (HTTP 404): {error_detail}"
            ) from error

        if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
            raise FatalTransportError(
                f"Fatal HTTP error {response.status_code}: {error_detail}"
            ) from error
        
        raise TransportError(
            f"HTTP error {response.status_code}: {error_detail}"
        ) from error

    return parse_json_response(response)


def get_request(
    server_url: str,
    endpoint: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    session: Optional[Session] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Trimite o cerere HTTP GET către serverul EDR.
    """
    url = build_url(server_url, endpoint)
    client = session or requests

    try:
        response = client.get(url, timeout=timeout, headers=headers)
        return handle_response(response)
    except Timeout as error:
        raise TransportError(f"GET request timed out: {url}") from error
    except RequestsConnectionError as error:
        raise TransportError(f"Could not connect to server: {url}") from error
    except RequestException as error:
        raise TransportError(f"GET request failed: {url}") from error


def post_request(
    server_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    session: Optional[Session] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Trimite o cerere HTTP POST cu payload JSON către serverul EDR.
    """
    url = build_url(server_url, endpoint)
    client = session or requests

    try:
        response = client.post(url, json=payload, timeout=timeout, headers=headers)
        return handle_response(response)
    except Timeout as error:
        raise TransportError(f"POST request timed out: {url}") from error
    except RequestsConnectionError as error:
        raise TransportError(f"Could not connect to server: {url}") from error
    except RequestException as error:
        raise TransportError(f"POST request failed: {url}") from error


def check_server_health(server_url: str) -> Dict[str, Any]:
    """
    Verifică dacă serverul EDR este pornit și accesibil.

    Apelează:
    GET /health
    """
    return get_request(server_url, "/health")


def register_agent(
    server_url: str,
    agent_payload: Dict[str, Any],
    agent_key: Optional[str] = None,
    enrollment_secret: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Înregistrează agentul endpoint la serverul EDR.

    Apelează:
    POST /api/agents/register

    Credențialele sunt opționale în semnătură, nu în practică: un server cu
    autentificare pornită refuză cu 401 o înregistrare fără niciuna. Valoarea
    implicită None există ca modulul să rămână utilizabil în testele care nu au
    treabă cu autentificarea.
    """
    return post_request(
        server_url,
        "/api/agents/register",
        agent_payload,
        headers=build_auth_headers(agent_key, enrollment_secret),
    )


def send_event(
    server_url: str,
    event_payload: Dict[str, Any],
    agent_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Trimite un eveniment de la agent către serverul EDR.

    Apelează:
    POST /api/events
    """
    return post_request(
        server_url,
        "/api/events",
        event_payload,
        headers=build_auth_headers(agent_key),
    )

def send_heartbeat(
        server_url: str,
        agent_id: str,
        heartbeat_payload: Optional[Dict[str, Any]] = None,
        agent_key: Optional[str] = None,
        ) -> Dict[str, Any]:
    """
    Trimite un heartbeat de la agent către serverul EDR.

    Apelează:
    POST /api/agents/{agent_id}/heartbeat
    """
    encoded_agent_id = quote(agent_id, safe="")
    payload = dict(heartbeat_payload) if heartbeat_payload else {}
    payload["agent_id"] = agent_id

    return post_request(
        server_url,
        f"/api/agents/{encoded_agent_id}/heartbeat",
        payload,
        headers=build_auth_headers(agent_key),
    )