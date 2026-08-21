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
import unittest
from pathlib import Path
from typing import Any, Dict, Optional


AGENT_REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_RELATIVE_PATH = Path("contracts") / "wire-contract.json"

# Repo-ul pereche și variabila care îi suprascrie locația. Convenția implicită
# este că cele două clone stau una lângă alta.
PEER_REPO_NAME = "edr-server"
PEER_PATH_ENV_VAR = "EDR_SERVER_PATH"

# Comută absența repo-ului pereche între skip și eșec.
#
#   "1"     — obligatoriu: absența e eșec de suită
#   "0"     — opțional: absența e skip
#   nesetat — obligatoriu dacă rulăm sub CI, opțional altfel
#
# De ce nu e simplu skip întotdeauna: verificările cross-repo sunt singurele
# care confruntă cele două părți ale contractului, iar un skip e o linie gri
# printre sute de puncte verzi. Pe o mașină de integrare care clonează un
# singur repo, exact testele care contează cel mai mult tac, iar raportul
# spune „totul verde".
#
# De ce nu e simplu eșec întotdeauna: o clonă singură trebuie să rămână
# testabilă. Cine ia doar acest repo și rulează suita nu a greșit cu nimic.
#
# CI (setat de GitHub Actions, GitLab, CircleCI, Azure și altele) desparte
# cele două situații fără ca nimeni să trebuiască să-și amintească ceva.
#
# Numele variabilei e identic cu cel din edr-server: aceeași regulă, același
# comutator, în ambele repo-uri.
PEER_REQUIRED_ENV_VAR = "EDR_REQUIRE_PEER_REPO"


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


def peer_repo_is_required() -> bool:
    """Dacă absența repo-ului pereche trebuie tratată ca eșec, nu ca skip."""
    configured = os.environ.get(PEER_REQUIRED_ENV_VAR)

    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes"}

    return bool(os.environ.get("CI"))


def require_peer_repo(what_goes_unverified: str) -> Path:
    """
    Repo-ul pereche, sau capătul testului — skip ori eșec, după mediu.

    Ridicarea lui SkipTest funcționează de oriunde, nu doar dintr-o metodă de
    test, deci locul de apel rămâne o singură linie. what_goes_unverified
    numește exact ce rămâne neverificat: mesajul ajunge și în raportul de skip,
    și în cel de eșec, iar cine îl citește trebuie să afle ce anume nu s-a
    verificat, nu doar că un fișier lipsea.
    """
    peer_repo = find_peer_repo()

    if peer_repo is not None:
        return peer_repo

    message = (
        f"{PEER_REPO_NAME} nu a fost găsit lângă agent, deci "
        f"{what_goes_unverified} rămâne neverificat în această rulare. "
        f"Clonează cele două repo-uri alături, sau indică {PEER_PATH_ENV_VAR} "
        f"către clona {PEER_REPO_NAME}."
    )

    if peer_repo_is_required():
        raise AssertionError(
            f"{message} Rulare marcată ca obligatorie "
            f"({PEER_REQUIRED_ENV_VAR}=1 sau CI setat). Dacă acest job chiar "
            f"clonează un singur repo, setează {PEER_REQUIRED_ENV_VAR}=0 — "
            f"explicit, ca golul să fie o decizie, nu o scăpare."
        )

    raise unittest.SkipTest(message)
