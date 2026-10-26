"""
Încarnarea procesului: identitatea rulării curente a agentului.

De ce stă într-un modul separat:
    Valoarea are doi cititori care nu au voie să ajungă la ea pe căi diferite.
    agent.py o pune în config, de unde o citesc builderii de payload, iar
    services/transport.py o pune pe antetul X-Agent-Instance-Id, lângă
    contoarele de fir. Dacă fiecare și-ar ține propria copie, antetul ar putea
    numi altă încarnare decât cea care chiar a numărat octeții. Un singur loc
    de unde citesc amândoi face nepotrivirea imposibilă prin construcție.

    Transportul nu poate importa agent.py: ar fi un import circular, iar
    `import agent` aduce cu el tot punctul de intrare. Nici registrul de fir nu
    e locul potrivit, deși are aceeași durată de viață. services/wire_ledger.py
    explică în „De ce încarnarea nu e un câmp aici” că registrul doar numără și
    nu se identifică, iar regula aceea rămâne adevărată. Încarnarea și
    contoarele se întâlnesc abia la emiterea anteturilor, în post_request().

De ce valoarea antetului vine de aici și nu din payload (edr-agent#21):
    Un eveniment rămas în spool de la o pornire anterioară își păstrează în
    corp `agent_instance_id` al încarnării care l-a creat. Octeții retransmiterii
    lui sunt însă numărați de registrul încarnării care îl trimite acum. Antetul
    trebuie să numească proprietarul contoarelor, adică procesul. Vezi
    METRICS.md §7.1.
"""

from uuid import uuid4


# Generată o singură dată, la importul modulului, adică o dată per proces.
# Stabilă pe durata rulării și diferită între rulări: exact cele două
# proprietăți de care depinde detecția de repornire de pe server.
_PROCESS_INSTANCE_ID: str = str(uuid4())


def process_instance_id() -> str:
    """Întoarce încarnarea procesului curent, aceeași la fiecare apel."""
    return _PROCESS_INSTANCE_ID
