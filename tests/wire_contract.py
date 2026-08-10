"""
Acces la contractul de fir partajat cu edr-server.

De ce există acest modul:
    Testele de contract comparau payload-urile agentului cu seturi de nume
    copiate de mână din schemele serverului. O copie pe care doar memoria unui
    om o actualizează nu verifică nimic: dacă serverul redenumește
    agent_instance_id în instance_id, payload-ul agentului devine greșit pe fir,
    dar toate testele trec mai departe, pentru că se compară cu numele vechi.

    contracts/wire-contract.json elimină copia. Fișierul e comis identic în
    ambele repo-uri și e singura sursă de adevăr pentru numele de pe fir:
    agentul își validează builderii față de el, serverul își validează modelele
    Pydantic față de el, iar ContractSyncTests confruntă cele două exemplare
    când ambele repo-uri sunt pe disc.

    Diferența față de o oglindă citită din codul serverului: acolo, o
    redenumire se vedea abia când cineva rula testele agentului. Aici, ea pică
    testele *în repo-ul care a făcut schimbarea*, în același commit.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


AGENT_REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_RELATIVE_PATH = Path("contracts") / "wire-contract.json"

# Repo-ul pereche și variabila care îi suprascrie locația. Convenția implicită
# este că cele două clone stau una lângă alta.
PEER_REPO_NAME = "edr-server"
PEER_PATH_ENV_VAR = "EDR_SERVER_PATH"


def load_contract(repo_root: Path = AGENT_REPO_ROOT) -> Dict[str, Any]:
    """Citește exemplarul de contract al unui repo."""
    contract_file = repo_root / CONTRACT_RELATIVE_PATH

    if not contract_file.is_file():
        raise FileNotFoundError(
            f"Contractul de fir lipsește: {contract_file}. Fără el, testele de "
            f"payload nu au față de ce valida."
        )

    # utf-8-sig, nu utf-8: editoarele Windows salvează des cu BOM, iar json.loads
    # crapă atunci cu o eroare care nu spune nimic despre contract. Fără BOM,
    # utf-8-sig se comportă identic cu utf-8.
    return json.loads(contract_file.read_text(encoding="utf-8-sig"))


CONTRACT = load_contract()


def contract_model(model_name: str) -> Dict[str, Any]:
    """Secțiunea unui model din contract, cu eroare explicită dacă lipsește."""
    models = CONTRACT["models"]

    if model_name not in models:
        raise LookupError(
            f"Modelul {model_name!r} nu există în contract. Modele declarate: "
            f"{sorted(models)}."
        )

    return models[model_name]


def required_fields(model_name: str) -> frozenset:
    """Câmpuri fără valoare implicită: emitătorul e obligat să le trimită."""
    return frozenset(contract_model(model_name)["required"])


def optional_fields(model_name: str) -> frozenset:
    return frozenset(contract_model(model_name)["optional"])


def forbidden_fields(model_name: str) -> frozenset:
    """Câmpuri a căror simplă prezență rupe o invariantă (vezi 'notes')."""
    return frozenset(contract_model(model_name)["forbidden"])


def declared_fields(model_name: str) -> frozenset:
    """Tot ce acceptă schema. Restul e aruncat de Pydantic la validare."""
    return required_fields(model_name) | optional_fields(model_name)


def find_peer_repo() -> Optional[Path]:
    """
    Localizează clona edr-server, dacă e disponibilă.

    Întoarce None când repo-ul pereche lipsește — agentul trebuie să rămână
    testabil dintr-o clonă singură. O cale explicită greșită e însă eroare, nu
    None: o variabilă de mediu scrisă greșit ar transforma verificarea de
    sincronizare exact în skip-ul tăcut pe care contractul îl elimină.
    """
    configured_path = os.environ.get(PEER_PATH_ENV_VAR)

    if configured_path:
        candidate = Path(configured_path)

        if not (candidate / CONTRACT_RELATIVE_PATH).is_file():
            raise RuntimeError(
                f"{PEER_PATH_ENV_VAR}={configured_path!r} nu conține "
                f"{CONTRACT_RELATIVE_PATH.as_posix()}. Corectează calea sau "
                f"elimină variabila."
            )

        return candidate

    sibling = AGENT_REPO_ROOT.parent / PEER_REPO_NAME

    return sibling if (sibling / CONTRACT_RELATIVE_PATH).is_file() else None
