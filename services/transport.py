import json
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests
from requests import Response, Session
from requests.exceptions import RequestException, Timeout, ConnectionError as RequestsConnectionError

from services.incarnation import process_instance_id
from services.wire_ledger import (
    CHANNEL_CONTROL,
    CHANNEL_ENROLLMENT,
    CHANNEL_EVENTS,
    WireLedger,
    WireLedgerSnapshot,
    get_wire_ledger,
)


DEFAULT_TIMEOUT_SECONDS = 5

# Antetele de autentificare. Numele trebuie să corespundă exact celor din
# edr-server/app/services/auth_service.py — o literă greșită aici nu produce o
# eroare de import, ci un 401 permanent care arată ca o cheie greșită.
AGENT_KEY_HEADER = "X-Agent-Key"
ENROLLMENT_SECRET_HEADER = "X-Enrollment-Secret"

# Tipul de conținut pe care îl punem noi, pentru că nu-l mai pune requests.
JSON_CONTENT_TYPE = "application/json"

# Anteturile de contabilizare: cât declară agentul că a pus pe fir până ACUM,
# adică **fără cererea curentă**.
#
# De ce fără ea, deși ar fi mai simplu cu tot:
#     Ca să declare dimensiunea cererii curente, agentul ar trebui să pună acea
#     dimensiune într-un antet al aceleiași cereri — dar antetul face parte din
#     cerere, deci îi schimbă dimensiunea. Circular. Raportarea decalată cu un
#     mesaj rupe bucla fără nicio aproximare: fiecare cifră descrie mesaje deja
#     încheiate.
#
# De ce anteturi și nu câmpuri în corp:
#     Un câmp în corp ar fi trebuit adăugat în fiecare schemă de payload și
#     validat de server ca parte a mesajului — adică un payload respins pentru
#     contabilitate greșită. Contabilizarea e despre transport, nu despre
#     conținut. Precedentul e stabilit de contracts/AUTH.md: ce descrie relația
#     agent-server stă în anteturi, ce descrie evenimentul stă în corp. De aceea
#     numele sunt aici și în METRICS.md §7, nu în wire-contract.json, care
#     descrie corpuri.
WIRE_ATTEMPTED_HEADER = "X-Agent-Wire-Attempted-Bytes"
WIRE_DELIVERED_HEADER = "X-Agent-Wire-Delivered-Bytes"

# Încarnarea care raportează cifrele de mai sus, adică a procesului care trimite.
# Nu e neapărat cea din corp: un eveniment retransmis din spool după o repornire
# poartă în corp încarnarea care l-a creat. Vezi build_incarnation_header().
#
# De ce e nevoie de ea pe antet, când corpul poartă oricum un agent_instance_id:
#     Serverul contabilizează în middleware, adică înainte de rutare, ca să nu
#     piardă cererile respinse cu 401 — exact traficul cel mai interesant. Un
#     middleware nu poate însă citi corpul fără să consume fluxul pe care îl
#     așteaptă ruta de după el. Antetul e singurul loc din care încarnarea se
#     poate afla la momentul acela.
#
# De ce contează atât de mult încarnarea:
#     Contoarele repornesc de la zero la fiecare pornire a agentului, prin
#     construcție (registrul e per încarnare). Fără să știe cui aparțin cifrele,
#     serverul ar vedea totalul raportat prăbușindu-se la zero în timp ce
#     propriul lui total crește — adică exact direcția gravă a discrepanței
#     (METRICS.md §7.2), declanșată fals la fiecare restart. Cu încarnarea pe
#     antet, resetul e așteptat și atribuibil, ca la detecția de repornire prin
#     agent_instance_id de la heartbeat.
WIRE_INSTANCE_HEADER = "X-Agent-Instance-Id"


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


def serialize_payload(payload: Dict[str, Any]) -> bytes:
    """
    Transformă un payload în exact octeții care pleacă pe fir.

    De ce există funcția asta, când `requests` știe deja să serializeze:
        Cu `json=payload`, serializarea se întâmplă înăuntrul bibliotecii, după
        ce agentul a predat obiectul. Agentul nu vede niciodată octeții pe care
        îi trimite, deci nu poate număra decât reserializând mesajul a doua
        oară — o estimare, nu o măsurătoare. METRICS.md §7 cere ca numărătorul
        afirmației centrale să fie măsurat. Aici e locul unde devine posibil:
        `len(serialize_payload(payload))` e exact ce trece prin socket.

    De ce e o singură funcție, folosită peste tot:
        Două locuri care serializează cu opțiuni diferite produc două cifre
        diferite pentru același mesaj, iar reconcilierea agent↔server ar acuza
        un canal necontabilizat acolo unde nu e decât o virgulă cu spațiu.
        Definiția octetului divulgat trăiește în funcția asta, nu în apelanți.

    De ce fix aceste opțiuni, și nu unele mai bune:
        Sunt exact cele pe care le folosea `requests` până acum
        (`json.dumps(obj, allow_nan=False)`, apoi encode UTF-8), deci comitul
        care introduce funcția nu mută nicio cifră de divulgare.

        - separatorii impliciți `(", ", ": ")` păstrează spațiul de după virgulă
          și după două puncte. `separators=(",", ":")` ar compacta mesajele,
          ceea ce e bun în sine, dar ar muta cifra de divulgare fără ca cineva
          să știe de ce. Compactarea, dacă se face, se face deliberat, în
          comitul ei, cu schimbarea de definiție consemnată în METRICS.md.
        - `ensure_ascii=True` (implicit) scapă caracterele non-ASCII în secvențe
          de forma backslash-u urmat de patru cifre hexa. O cale cu diacritice
          devine astfel mai lungă în octeți decât pare în caractere — și e
          corect așa: octeții aceia chiar pleacă de pe endpoint.
        - `allow_nan=False` refuză NaN și Infinity, exact ca `requests`. JSON-ul
          standard nu le are, iar serverul le-ar refuza oricum.
    """
    return json.dumps(payload, allow_nan=False).encode("utf-8")


def build_json_headers(headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Adaugă `Content-Type: application/json` peste antetele primite.

    Cu `json=payload`, antetul îl punea `requests`. Odată ce trimitem un corp
    deja serializat prin `data=`, biblioteca nu mai are de unde ști că e JSON și
    nu mai pune nimic — iar serverul ar refuza cererea. E jumătatea invizibilă a
    trecerii la serializare explicită, și singurul mod în care pasul ăsta putea
    strica fiecare rută deodată.

    Antetul dat de apelant nu se suprascrie (comparație insensibilă la
    majuscule, ca în `CaseInsensitiveDict`), la fel cum nu-l suprascria nici
    `requests`. Dicționarul primit nu se modifică pe loc: apelanții îl
    construiesc cu build_auth_headers() și n-au motiv să-l vadă crescând.
    """
    request_headers = dict(headers) if headers else {}

    if not any(name.lower() == "content-type" for name in request_headers):
        request_headers["Content-Type"] = JSON_CONTENT_TYPE

    return request_headers


def build_wire_headers(snapshot: WireLedgerSnapshot) -> Dict[str, str]:
    """
    Anteturile de contabilizare, dintr-o fotografie luată înainte de trimitere.

    Primește fotografia, nu registrul, tocmai ca să nu poată include din greșeală
    cererea curentă: cine cheamă funcția a decis deja despre ce moment vorbește.

    Zero se trimite explicit, nu se omite. Prima cerere a unei încarnări poartă
    două zerouri, iar asta e o afirmație verificabilă — spre deosebire de un
    antet lipsă, care nu se distinge de un agent vechi, de un intermediar care
    taie anteturi, sau de un bug. Același raționament ca la zero-ul din blocul
    `disclosure`.

    Totalul e peste canale, nu pe canal. Serverul numără octeții primiți de la o
    încarnare fără să-i despartă — el vede corpuri și rute, nu categoriile
    noastre. Separarea pe canale rămâne locală, pentru afirmațiile din
    METRICS.md; reconcilierea are nevoie de un singur număr comparabil.
    """
    return {
        WIRE_ATTEMPTED_HEADER: str(snapshot.totals.attempted_bytes),
        WIRE_DELIVERED_HEADER: str(snapshot.totals.delivered_bytes),
    }


def build_incarnation_header(
    agent_instance_id: Optional[str] = None,
) -> Dict[str, str]:
    """
    Antetul cu încarnarea procesului care trimite (METRICS.md §7.1).

    De ce procesul și nu payload-ul (edr-agent#21):
        Antetul trebuie să numească proprietarul contoarelor de lângă el, iar
        contoarele sunt ale registrului procesului curent — post_request() le ia
        din wire_ledger.snapshot(). Prima formă citea încarnarea din payload, ca
        antetul și corpul să nu se poată contrazice. Pentru un eveniment rămas
        în spool de la o pornire anterioară regula aceea dădea încarnarea
        greșită: corpul păstrează încarnarea care l-a creat, dar octeții
        retransmiterii sunt numărați de încarnarea care îl trimite. Serverul
        punea astfel octeții la o încarnare și raportul la alta, iar
        reconcilierea arăta un below_lower_bound fals exact după repornirile cu
        spool nevid.

    Antetul și corpul POT deci diferi, la retransmiterea din spool:
        Diferența nu e o contradicție, ci informație. Corpul spune cine a
        observat evenimentul, antetul cine l-a pus pe fir. Serverul
        contabilizează după antet și stochează evenimentul după corp, iar
        niciuna dintre cele două nu o corectează pe cealaltă. De aceea funcția
        nici nu mai primește payload-ul: corpul nu e o sursă pentru antet, iar
        post_request() nu-l modifică.

    Înregistrarea nu mai e un caz aparte:
        contracts/wire-contract.json INTERZICE `agent_instance_id` în corpul
        înregistrării, pentru că ar suprascrie baseline-ul de repornire înainte
        ca heartbeat-ul să îl compare. Interdicția e pe CORP, nu pe cerere:
        antetul nu trece prin schema serverului și nu ajunge în agents_store.
        Încarnarea pleacă și acolo pe antet, din aceeași sursă ca la orice altă
        cerere.

    De ce antetul e mereu prezent:
        Încarnarea procesului există de la import, deci nu mai e nimic de omis.
        Octeții rămân neatribuibili doar la prima înrolare, pentru că lipsește
        încă o cheie de agent (METRICS.md §7.1), nu pentru că lipsește antetul.

    `agent_instance_id` e doar o suprascriere, pentru teste. Un șir gol contează
    ca absent și lasă loc încarnării procesului, ca un antet gol să nu poată
    strica o cheie de contabilizare pe server.
    """
    instance_id = agent_instance_id or process_instance_id()

    return {WIRE_INSTANCE_HEADER: str(instance_id)}


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
    *,
    channel: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    session: Optional[Session] = None,
    headers: Optional[Dict[str, str]] = None,
    ledger: Optional[WireLedger] = None,
    agent_instance_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Trimite o cerere HTTP POST cu payload JSON către serverul EDR.

    Corpul e serializat aici, explicit, nu de `requests` prin `json=`. Vezi
    serialize_payload() pentru motiv: fără octeții văzuți de agent, numărătorul
    de divulgare nu poate fi decât o estimare.

    `channel` e obligatoriu și nu are valoare implicită, deliberat. O valoare
    implicită ar însemna că un apelant viitor care uită să se gândească la
    canal își vede octeții aterizând tăcut în găleata altcuiva — cel mai probabil
    peste evenimente, adică peste exact cifra afirmației principale. Fără
    implicit, un apel nou nu trece până nu răspunde la întrebare.

    E și keyword-only, împreună cu restul argumentelor opționale. Prima formă îl
    avea pozițional, pe locul patru, și a intrat imediat peste `headers` în
    fake-urile din tests/test_api_authentication.py. Un argument nou strecurat
    în mijlocul unei liste poziționale se leagă tăcut de alt parametru, iar aici
    consecința ar fi fost un canal folosit drept antet — exact tipul de eroare
    pe care o metrică nu are voie s-o aibă.

    `ledger` există doar pentru teste: implicit se folosește registrul
    încarnării, cel de la nivel de modul în services/wire_ledger.py.

    `agent_instance_id` există tot doar pentru teste: suprascrie încarnarea
    procesului pe antet. Implicit antetul poartă încarnarea procesului care
    trimite, nu pe cea din payload, iar payload-ul pleacă neschimbat. Vezi
    build_incarnation_header().
    """
    url = build_url(server_url, endpoint)
    client = session or requests
    wire_ledger = ledger or get_wire_ledger()

    try:
        # Serializarea stă înăuntrul lui try deliberat: până acum o făcea tot
        # `requests`, tot aici, iar un payload neserializabil ieșea ca
        # InvalidJSONError — adică RequestException — și devenea TransportError,
        # eroare temporară. Păstrăm clasificarea neschimbată, pentru că acest
        # comit nu are voie să mute nimic observabil. Dacă un mesaj imposibil de
        # serializat merită tratat ca poison message (FatalTransportError), e o
        # decizie separată, cu comitul ei.
        try:
            body = serialize_payload(payload)
        except (ValueError, TypeError) as error:
            raise TransportError(
                f"POST payload could not be serialized: {url}"
            ) from error

        # Fotografia se ia ÎNAINTE de record_attempt, nu după: anteturile poartă
        # totalul de dinaintea cererii curente, altfel ar declara dimensiunea
        # mesajului în care se află.
        #
        # Sub concurență, fotografia poate rămâne cu un mesaj-două în urmă față
        # de ce a plecat efectiv, dacă celălalt fir înregistrează între timp.
        # Nu încercăm să facem fotografia și incrementarea atomice, pentru că
        # n-ar rezolva problema reală: două mesaje pot pleca într-o ordine și
        # ajunge în alta, deci decalajul rămâne oricum. Consecința se declară,
        # nu se ascunde — toleranța reconcilierii nu e „un mesaj în zbor", ci
        # „câte mesaje pot fi simultan în zbor" (azi: dispecerul plus bucla de
        # heartbeat).
        request_headers = build_json_headers(headers)
        request_headers.update(build_wire_headers(wire_ledger.snapshot()))
        request_headers.update(build_incarnation_header(agent_instance_id))

        # Momentul „a părăsit endpoint-ul", din METRICS.md §1.1. Se numără
        # înainte de trimitere și nu se scade niciodată: dacă cererea moare
        # într-un timeout, octeții tot au plecat. Un contor incrementat după
        # răspuns ar raporta zero divulgare exact în scenariul în care agentul
        # trimite fără oprire și nu primește nimic înapoi.
        wire_ledger.record_attempt(channel, len(body))

        response = client.post(
            url,
            data=body,
            timeout=timeout,
            headers=request_headers,
        )

        # Momentul „a ajuns la server", înregistrat ÎNAINTE de handle_response,
        # nu după. Poziția e toată ideea: handle_response ridică excepție la 401,
        # 403, 409 și 422, iar o înregistrare de după ea ar trata fiecare refuz
        # ca pe o nelivrare. Ca să te poată refuza, serverul trebuie mai întâi
        # să fi primit mesajul — iar un agent cu cheia greșită care trimite ore
        # în șir e exact traficul pe care metrica trebuie să-l arate, nu să-l
        # ascundă.
        wire_ledger.record_delivery(channel, len(body))

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
    agent_instance_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Înregistrează agentul endpoint la serverul EDR.

    Apelează:
    POST /api/agents/register

    Credențialele sunt opționale în semnătură, nu în practică: un server cu
    autentificare pornită refuză cu 401 o înregistrare fără niciuna. Valoarea
    implicită None există ca modulul să rămână utilizabil în testele care nu au
    treabă cu autentificarea.

    `agent_instance_id` e suprascrierea din post_request(), tot doar pentru
    teste. Agentul nu-l dă: încarnarea pleacă pe antet din procesul curent.
    """
    return post_request(
        server_url,
        "/api/agents/register",
        agent_payload,
        channel=CHANNEL_ENROLLMENT,
        headers=build_auth_headers(agent_key, enrollment_secret),
        agent_instance_id=agent_instance_id,
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
        channel=CHANNEL_EVENTS,
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
        channel=CHANNEL_CONTROL,
        headers=build_auth_headers(agent_key),
    )