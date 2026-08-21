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

    if hash_status == "ok" and file_size is None:
        raise ValueError(
            "hash_status este 'ok' dar file_size lipseste. file_size e "
            "numaratorul metricii de octeti divulgati vs. always-upload si nu "
            "se poate reconstrui retroactiv."
        )

    current_time = datetime.now(timezone.utc).isoformat()
    event_time = occurred_at or current_time
    normalized_path = normalize_file_path(file_path)

    descriptions = {
        "file_created": "New file detected in monitored directory",
        "file_modified": "File modified in monitored directory",
    }

    description = descriptions.get(event_type, "File system event detected")

    payload: Dict[str, Any] = {
        "client_event_id": str(uuid4()),
        "agent_id": agent_id,
        "agent_instance_id": agent_instance_id,
        "event_type": event_type,
        "occurred_at": event_time,
        "file_path": normalized_path,
        "description": f"{description} at {event_time}",
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

    return payload