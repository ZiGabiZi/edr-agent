from typing import Any, Dict, Optional
from urllib.parse import quote

import requests
from requests import Response, Session
from requests.exceptions import RequestException, Timeout, ConnectionError as RequestsConnectionError


DEFAULT_TIMEOUT_SECONDS = 5


class TransportError(Exception):
    """Eroare ridicată atunci când comunicarea cu serverul EDR eșuează."""
    pass

class FatalTransportError(TransportError):
    """
    Eroare irecuperabilă (ex: 401 Unauthorized, 403 Forbidden, 409 Conflict).
    Agentul trebuie să se oprească imediat, deoarece continuarea ar fi inutilă.
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


def handle_response(response: Response) -> Dict[str, Any]:
    """
    Verifică status code-ul HTTP și întoarce răspunsul JSON.

    Dacă serverul întoarce eroare 4xx/5xx, se ridică TransportError.
    """
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        try:
            error_detail = response.json()
        except ValueError:
            error_detail = response.text

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
) -> Dict[str, Any]:
    """
    Trimite o cerere HTTP GET către serverul EDR.
    """
    url = build_url(server_url, endpoint)
    client = session or requests

    try:
        response = client.get(url, timeout=timeout)
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
) -> Dict[str, Any]:
    """
    Trimite o cerere HTTP POST cu payload JSON către serverul EDR.
    """
    url = build_url(server_url, endpoint)
    client = session or requests

    try:
        response = client.post(url, json=payload, timeout=timeout)
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


def register_agent(server_url: str, agent_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Înregistrează agentul endpoint la serverul EDR.

    Apelează:
    POST /api/agents/register
    """
    return post_request(server_url, "/api/agents/register", agent_payload)


def send_event(server_url: str, event_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Trimite un eveniment de la agent către serverul EDR.

    Apelează:
    POST /api/events
    """
    return post_request(server_url, "/api/events", event_payload)

def send_heartbeat(
        server_url: str,
        agent_id: str,
        heartbeat_payload: Optional[Dict[str, Any]] = None
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
        payload
    )