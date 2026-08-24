"""
Identitatea mașinii — agent_id derivat o dată și înghețat pe disc.
==================================================================

Problema pe care o rezolvă:
    config.json avea agent_id scris fix. Al doilea endpoint desfășurat cu același
    fișier revendica un nume deja legat de altă amprentă de mașină, serverul
    întorcea 409, iar agentul se oprea la pornire. Simptomul e cel mai prost cu
    putință: mașina care ar trebui monitorizată nu apare deloc în consolă.

Întrebarea care decide forma soluției nu e „de unde derivăm", ci
„o dată sau la fiecare pornire":

    Stabilitatea sursei contează doar dacă derivarea se recalculează la fiecare
    boot. Derivat o dată și înghețat, un hostname instabil nu mai e o problemă —
    a fost fotografiat la înrolare și nu se mai mișcă.

De ce înghețarea NU e opțională:
    agent_id e legat de cheia de API emisă pentru el. Dacă s-ar recalcula la
    fiecare pornire și componenta lui s-ar schimba (cineva redenumește mașina):

      1. agentul derivă un agent_id nou;
      2. pe disc are agent_key, emisă pentru numele vechi;
      3. register trimite cheia împreună cu noul agent_id;
      4. secretul de înrolare a fost consumat, deci authorize_registration cade
         pe a doua cale: agent_id_for_key(cheie) întoarce numele VECHI,
         require_identity_match compară cu cel nou -> 403.

    Iar 403 e, prin decizia luată la pasul de autentificare, exact eroarea care
    nu se repară niciodată singură. Agentul ar rămâne blocat, cu coada crescând,
    până la reprovizionarea manuală a secretului de înrolare pe acea mașină. O
    redenumire de mașină ar deveni un blocaj permanent al identității.

Unde se îngheață — NU în config.json:
    Două motive. Întâi, config.json e fișierul copiat între mașini, deci
    scrierea acolo ar reproduce exact bug-ul de la care am plecat, la
    următoarea desfășurare. Al doilea, config.json e configurație de operator,
    nu stare locală de mașină. Fișierul de identitate stă lângă agent_key, cu
    aceeași proprietate: procedura de instalare copiază config.json și
    enrollment_secret, nu copiază niciodată starea locală.

Forma aleasă — hostname-primele8dinhash:
    Nucleul determinist e amprenta mașinii; hostname-ul e doar prefix lizibil.
    Ordinea contează în raționament, nu în șirul rezultat: partea din hash
    garantează unicitatea și, mai important, IDEMPOTENȚA la reinstalare. Dacă
    fișierul de identitate se pierde și agentul se reinstalează pe aceeași
    mașină, derivarea produce același agent_id, deci aceeași înregistrare pe
    server, fără dublură. Un identificator complet aleator ar fi fost la fel de
    unic, dar ar fi creat o înregistrare nouă la fiecare reinstalare.

Tensiunea acceptată conștient:
    Hostname în ID înseamnă că derivarea NU mai e idempotentă dacă mașina e și
    redenumită, și reinstalată. Rezultatul e o înregistrare duplicat, nu un
    blocaj, iar combinația e rară. Lizibilitatea în consolă merită prețul.
"""

import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any, Dict, Optional


_BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_AGENT_ID_PATH = _BASE_DIR / "agent_id"

# agent_id ajunge într-o cale de URL (services/transport.py, quote(agent_id,
# safe="")) și în consola operatorului. Un hostname poate conține puncte,
# spații sau diacritice, care ies urât în ambele. Se reduce la un set sigur.
_UNSAFE_CHARACTERS = re.compile(r"[^a-z0-9-]+")
_REPEATED_DASHES = re.compile(r"-{2,}")

_MAX_HOSTNAME_PART = 24
_MACHINE_FINGERPRINT_LENGTH = 8
_FALLBACK_HOSTNAME_PART = "endpoint"


logger = logging.getLogger(__name__)


class AgentIdentityError(Exception):
    """Eroare la citirea sau scrierea fișierului de identitate."""


def normalize_id_part(value: Optional[str]) -> str:
    """
    Reduce o bucată de identificator la minuscule și [a-z0-9-].

    Nu e cosmetică: valoarea ajunge într-o cale de URL și într-un jurnal citit
    de om. Un hostname de forma DESKTOP-6VOR261.local ar produce altfel un
    agent_id cu punct, iar unul cu diacritice ar produce secvențe procentuale
    în URL, imposibil de căutat în loguri.
    """
    if not value:
        return ""

    normalized = _UNSAFE_CHARACTERS.sub("-", value.strip().lower())
    normalized = _REPEATED_DASHES.sub("-", normalized).strip("-")

    return normalized


def derive_agent_id(
    hostname: Optional[str],
    machine_id_hash: Optional[str],
) -> str:
    """
    Compune agent_id-ul din prefixul lizibil și nucleul determinist.

    machine_id_hash absent nu e o eroare: get_stable_machine_identifier()
    întoarce ("unknown", None) când toate cele trei surse eșuează, iar
    hash_identifier(None) întoarce None. Atunci nucleul determinist lipsește, și
    singurul răspuns corect e un fragment aleator — tocmai pentru că rezultatul
    se îngheață oricum, deci lipsa de determinism nu se mai vede după prima
    pornire. Alternativa, un nume fix de tipul unknown, ar fi garantat coliziuni
    între toate mașinile fără identificator stabil.
    """
    hostname_part = normalize_id_part(hostname)[:_MAX_HOSTNAME_PART].strip("-")

    if not hostname_part:
        hostname_part = _FALLBACK_HOSTNAME_PART

    if machine_id_hash:
        fingerprint = machine_id_hash[:_MACHINE_FINGERPRINT_LENGTH].lower()
    else:
        # token_hex(4) -> 8 caractere hexazecimale, aceeași lățime ca prefixul
        # de hash, ca cele două cazuri să arate identic în consolă.
        fingerprint = secrets.token_hex(_MACHINE_FINGERPRINT_LENGTH // 2)

    return f"{hostname_part}-{normalize_id_part(fingerprint)}"


def _read_frozen_agent_id(path: Path) -> Optional[str]:
    if not path.exists():
        return None

    try:
        # utf-8-sig: fișierul poate fi inspectat sau rescris de un operator pe
        # Windows, unde uneltele obișnuite adaugă BOM. Aceeași lecție ca la
        # fișierele de credențiale.
        content = path.read_text(encoding="utf-8-sig").replace("﻿", "").strip()
    except (OSError, UnicodeDecodeError) as error:
        logger.error(
            "Could not read the agent identity at %s: %s. A new identity will be "
            "derived, which may create a duplicate registration on the server.",
            path,
            error,
        )
        return None

    return content or None


def _freeze_agent_id(path: Path, agent_id: str) -> None:
    """
    Scrie identitatea atomic: fișier temporar, apoi replace.

    Scrierea directă ar putea lăsa, la o oprire nefericită, un fișier trunchiat
    — adică o identitate parțială, care la următoarea pornire ar fi citită ca
    validă și ar produce o a doua înregistrare pe server, cu un nume aproape la
    fel. Un fișier care nu există e recuperabil prin derivare; unul greșit, nu.

    Spre deosebire de agent_key, aici NU se restrâng permisiunile: agent_id nu e
    un secret, e un nume care oricum apare în fiecare cerere și în consolă.
    """
    temporary_path = path.with_name(path.name + ".tmp")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(agent_id + "\n", encoding="utf-8")
        os.replace(temporary_path, path)
    except (OSError, ValueError) as error:
        # ValueError, nu doar OSError: calea vine din config.json prin
        # agent_id_path, iar JSON poate purta un octet nul, care face ca
        # os.mkdir să ridice ValueError înainte să atingă discul. Netratată, ar
        # fi ieșit din resolve_agent_id și ar fi oprit agentul la pornire — adică
        # exact eșecul pe care funcția asta există ca să-l evite.
        try:
            temporary_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

        raise AgentIdentityError(
            f"Could not write the agent identity to {path}: {error}"
        ) from error


def resolve_agent_id(
    config: Dict[str, Any],
    system_info: Dict[str, Any],
    path: Optional[Path] = None,
) -> str:
    """
    Întoarce identitatea acestei mașini, în ordinea de precedență:

        1. agent_id explicit în config.json — decizia operatorului bate tot, iar
           fișierul de identitate NU se scrie. Cine fixează un nume vrea acel
           nume, nu vrea să înghețe altul pe lângă el.
        2. fișierul de identitate deja înghețat — calea normală, de la a doua
           pornire încolo.
        3. derivare din hostname și amprenta mașinii, urmată de înghețare.

    Contrastul cu ensure_agent_instance_id() merită observat, pentru că sunt
    exact opuse și stau una lângă alta:
        - incarnarea IGNORĂ deliberat o valoare din config.json, pentru că
          trebuie să difere la fiecare rulare;
        - identitatea PREFERĂ valoarea din config.json, pentru că trebuie să
          rămână aceeași cât timp mașina e aceeași.
    """
    identity_path = Path(path or config.get("agent_id_path") or DEFAULT_AGENT_ID_PATH)

    configured = config.get("agent_id")
    if isinstance(configured, str) and configured.strip():
        agent_id = configured.strip()
        config["agent_id"] = agent_id
        logger.info(
            "Agent id taken from configuration: %s. The identity file at %s is "
            "not consulted and not written.",
            agent_id,
            identity_path,
        )
        return agent_id

    frozen = _read_frozen_agent_id(identity_path)
    if frozen:
        config["agent_id"] = frozen
        logger.info("Agent id loaded from %s: %s", identity_path, frozen)
        return frozen

    hostname = system_info.get("hostname")
    machine_id_hash = system_info.get("machine_id_hash")
    agent_id = derive_agent_id(hostname, machine_id_hash)

    if machine_id_hash:
        logger.info(
            "Derived agent id %s from hostname and machine fingerprint.", agent_id
        )
    else:
        logger.warning(
            "Derived agent id %s with a RANDOM suffix: this machine exposes no "
            "stable identifier (machine_id_type=%s). Reinstalling the agent after "
            "losing %s will produce a second registration on the server.",
            agent_id,
            system_info.get("machine_id_type"),
            identity_path,
        )

    try:
        _freeze_agent_id(identity_path, agent_id)
        logger.info("Agent identity frozen at %s.", identity_path)
    except AgentIdentityError as error:
        # NU e motiv de oprire. Agentul funcționează complet în rularea asta;
        # riscul e doar o identitate rederivată la următoarea pornire, iar
        # aceea e stabilă atâta timp cât hostname-ul și amprenta nu se schimbă.
        logger.error(
            "%s The identity holds for this run only and will be derived again "
            "at the next start.",
            error,
        )

    config["agent_id"] = agent_id
    return agent_id
