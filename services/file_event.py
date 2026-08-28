"""
Construcția payload-ului unui eveniment de fișier.
===================================================

Extras din services/file_monitor.py la introducerea firului de hashing.
Motivul e strict de dependențe: FileHasher construiește acum payload-ul final,
iar FileMonitor deține hasher-ul — dacă builder-ul rămânea în file_monitor,
cele două module s-ar fi importat reciproc.

file_monitor reexportă numele de aici, deci apelanții și testele existente nu
se schimbă.
"""

import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
from uuid import uuid4


FileEventCallback = Callable[[Dict[str, str]], None]

# Treapta la care se inchide un eveniment de fisier. Azi exista una singura:
# amprenta pleaca, continutul nu. Constanta e aici, nu literala la apel, ca
# introducerea lui T1 sa fie o schimbare intr-un singur loc.
DEFAULT_DISCLOSURE_TIER = "T0"

# Vocabularul inchis al treptelor. Oglindeste VALID_DISCLOSURE_TIERS din schema
# serverului, iar amandoua sunt legate de notele contractului prin test
# (tests/test_disclosure_tier.py), ca sa nu poata diverge in tacere — aceeasi
# disciplina ca la vocabularul lui hash_status.
VALID_DISCLOSURE_TIERS = frozenset({"T0", "T1", "T2", "T3"})

# Trepte la care continutul fisierului nu are voie sa fi plecat. Oglindeste
# _CONTENTLESS_TIERS din schema serverului.
_CONTENTLESS_TIERS = frozenset({"T0", "T1"})


def normalize_file_path(file_path: str) -> str:
    """
    Normalizează calea unui fișier pentru raportare și comparare.

    Funcționează atât pe Windows, cât și pe Linux.
    """
    return os.path.abspath(file_path)


def build_file_event_payload(
    agent_id: str,
    event_type: str,
    file_path: str,
    agent_instance_id: str,
    occurred_at: Optional[str] = None,
    settle_wait_ms: Optional[int] = None,
    sha256: Optional[str] = None,
    hash_status: Optional[str] = None,
    file_size: Optional[int] = None,
    hash_duration_ms: Optional[int] = None,
    tier: str = DEFAULT_DISCLOSURE_TIER,
    content_bytes: int = 0,
) -> Dict[str, Any]:
    """
    Construiește payload-ul unui eveniment de fișier.

    occurred_at este momentul PRIMEI observații a fișierului, furnizat de
    SettleTracker (Decizia 1). Trebuie transmis explicit tocmai pentru că
    raportarea are loc cu întârziere, după stabilizare: calculat aici, ar fi
    momentul emiterii, nu al faptului, iar serverul ar ordona evenimentele
    greșit — exact eroarea pe care câmpul occurred_at a fost introdus s-o
    prevină (vezi nota din contracts/wire-contract.json).

    Rămâne opțional pentru compatibilitate cu apelurile care nu trec încă prin
    tracker; absent, se folosește ceasul de acum.

    settle_wait_ms și hash_duration_ms călătoresc sub measurements — model
    separat structural tocmai ca să nu se amestece cu câmpurile care descriu
    fișierul. Niciun câmp de acolo nu are voie să intre într-o decizie de
    verdict sau de escaladare.

    Invarianta contractului sha256 prezent <=> hash_status == 'ok' este
    verificată aici, nu doar pe server. Un payload care o încalcă ar primi 422,
    iar EventDispatcher l-ar trata ca poison message și l-ar șterge din spool:
    pierdere de date, nu câmp gol. Mai bine eșuăm zgomotos la construcție,
    unde stack trace-ul arată direct apelantul vinovat.
    """
    if (sha256 is not None) != (hash_status == "ok"):
        raise ValueError(
            f"Invarianta contractului incalcata: sha256="
            f"{'prezent' if sha256 is not None else 'absent'} cu "
            f"hash_status={hash_status!r}. sha256 prezent <=> hash_status == 'ok'."
        )

    # Cele trei verificari de mai jos sunt oglinda exacta a lui
    # EventDisclosure._validate_tier de pe server, in aceeasi ordine. Duplicarea
    # e deliberata, dar trebuie sa fie COMPLETA ca sa-si merite rostul: orice
    # verificare pe care serverul o face si agentul nu, se transforma intr-un
    # 422 -> FatalTransportError -> EventDispatcher trateaza evenimentul ca
    # poison message si il sterge din spool. Pierdere de date, nu camp gol.
    # Aici esueaza la constructie, cu stack trace catre apelantul vinovat.
    if tier not in VALID_DISCLOSURE_TIERS:
        raise ValueError(
            f"tier invalid: {tier!r}. "
            f"Valori acceptate: {sorted(VALID_DISCLOSURE_TIERS)}."
        )

    if content_bytes < 0:
        raise ValueError(
            f"content_bytes nu poate fi negativ: {content_bytes}."
        )

    if tier in _CONTENTLESS_TIERS and content_bytes > 0:
        raise ValueError(
            f"tier este {tier!r} dar content_bytes este {content_bytes}. "
            f"Treptele T0 si T1 nu divulga continut prin definitie; un numar "
            f"pozitiv aici inseamna ori un bug de atribuire, ori un canal "
            f"necontabilizat."
        )

    if hash_status == "ok" and file_size is None:
        raise ValueError(
            "hash_status este 'ok' dar file_size lipseste. file_size e "
            "numaratorul metricii de octeti divulgati vs. always-upload si nu "
            "se poate reconstrui retroactiv."
        )

    current_time = datetime.now(timezone.utc).isoformat()
    event_time = occurred_at or current_time
    normalized_path = normalize_file_path(file_path)

    # description NU se emite pentru evenimentele de fișier, deliberat.
    #
    # Era un tabel de căutare pe event_type plus timestamp-ul — adică o
    # reformulare în engleză a exact ceea ce spun deja `event_type` și
    # `occurred_at`. Informație nouă: zero.
    #
    # Nu e o economie cosmetică. Măsurătoarea liniei de bază T0 a arătat că
    # acel câmp era CEL MAI SCUMP din payload: 93 din 568 de octeți, adică
    # 16,4%, mai mult decât amprenta SHA-256 însăși. Iar canalul de evenimente
    # circulă la FIECARE fișier atins, nu doar la cele escaladate, deci costul
    # se plătea de sute de ori pe minut sub o rafală.
    #
    # Câmpul rămâne opțional în contract (v4) și rămâne folosit acolo unde chiar
    # poartă informație: evenimentele de ciclu de viață ale agentului, și
    # narațiunea de repornire pe care o compune serverul. Distincția e între un
    # text AUTORAT și unul DERIVAT — al doilea nu are ce căuta pe fir.
    payload: Dict[str, Any] = {
        "client_event_id": str(uuid4()),
        "agent_id": agent_id,
        "agent_instance_id": agent_instance_id,
        "event_type": event_type,
        "occurred_at": event_time,
        "file_path": normalized_path,
    }

    if sha256 is not None:
        payload["sha256"] = sha256

    if hash_status is not None:
        payload["hash_status"] = hash_status

    if file_size is not None:
        payload["file_size"] = file_size

    measurements: Dict[str, Any] = {}

    if settle_wait_ms is not None:
        measurements["settle_wait_ms"] = settle_wait_ms

    if hash_duration_ms is not None:
        measurements["hash_duration_ms"] = hash_duration_ms

    if measurements:
        payload["measurements"] = measurements

    # Blocul insoteste exact evenimentele de fisier — vezi bicondiționalitatea
    # din contract. Aici e neconditionat pentru ca acest builder construieste
    # DOAR evenimente de fisier; cele de ciclu de viata au builderii lor in
    # agent.py si raman, corect, fara treapta.
    #
    # content_bytes se emite si cand e zero, spre deosebire de campurile
    # optionale de mai sus: un zero explicit la T0 e o afirmatie verificabila
    # ("niciun octet de continut n-a plecat"), pe cand absenta ar fi
    # ambigua intre "zero" si "nu s-a masurat".
    payload["disclosure"] = {"tier": tier, "content_bytes": content_bytes}

    return payload