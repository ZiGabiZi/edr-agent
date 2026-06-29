"""
Strategie de Exponential Backoff cu Jitter Hibrid Agent-Ancorat
================================================================

Modulul implementează un mecanism de reconnectare adaptivă pentru agentul EDR,
proiectat specific pentru scenariul în care mai mulți agenți dintr-un parc de
endpoint-uri pierd simultan conexiunea cu serverul.

Problema clasică — Thundering Herd:
    Fără o strategie de backoff, toți agenții care detectează că serverul este
    indisponibil vor reîncerca la exact același interval (heartbeat_interval_seconds),
    bombardând serverul cu un val de cereri simultane imediat ce acesta revine online.
    Această problemă se amplifică în contextul EDR, unde un vârf brusc de trafic
    poate declanșa el însuși alerte de securitate pe server.

Soluția implementată — Jitter Hibrid Agent-Ancorat:
    Combină două componente de jitter pentru a distribui natural tentativele de
    reconectare, fără coordonare centralizată între agenți:

    1. Componentă DETERMINISTĂ (φ_agent):
       Derivată din hash-ul SHA-256 al agent_id. Fiecare agent are o „fază" de
       reconectare unică și stabilă, care se păstrează și după reporniri. Doi agenți
       cu ID-uri diferite vor genera automat timpi de reconectare distribuiți, fără
       nicio comunicare între ei.

    2. Componentă ALEATORIE (ε_random):
       Adaugă variație suplimentară pentru a preveni coliziunile exacte, inclusiv
       între agenți cu ID-uri consecutive sau structurate similar.

    Formula finală:
        W = min(W_max, W_base × 2ⁿ) × (1 + φ_agent + ε_random)

        unde:
            n         — numărul de eșecuri consecutive (0-indexat)
            φ_agent   ∈ [0, jitter_ratio/2)  — faza deterministă a agentului
            ε_random  ∈ [0, jitter_ratio/2)  — variație aleatorie suplimentară

    Exemplu pentru un agent cu heartbeat_interval=10s și jitter_ratio=0.20:
        Eșec 1 → W_base =  10s → W_final ≈  10s–12s  (funcție de agent_id și random)
        Eșec 2 → W_base =  20s → W_final ≈  20s–24s
        Eșec 3 → W_base =  40s → W_final ≈  40s–48s
        Eșec 4 → W_base =  80s → W_final ≈  80s–96s
        Eșec 5 → W_base = 160s → W_final ≈ 160s–192s
        Eșec 6 → W_base = 250s → W_final ≈ 250s–300s  (plafon atins)

Relevanță pentru medii air-gapped:
    Într-o rețea izolată, nu există o componentă centrală care să coordoneze
    reconectarea agenților. Componenta deterministă rezolvă această problemă
    prin distribuție implicită bazată exclusiv pe identitatea fiecărui agent.
"""

import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Constante implicite
# ---------------------------------------------------------------------------

DEFAULT_BASE_DELAY_SECONDS: float = 10.0
DEFAULT_MAX_DELAY_SECONDS: float = 300.0   # plafon: 5 minute
DEFAULT_MULTIPLIER: float = 2.0
DEFAULT_JITTER_RATIO: float = 0.2          # variație maximă totală: 20%


# ---------------------------------------------------------------------------
# Funcții pure — ușor de testat independent de starea controlerului
# ---------------------------------------------------------------------------

def _compute_agent_phase(agent_id: str, jitter_ratio: float) -> float:
    """
    Derivă o fază de jitter deterministă din hash-ul SHA-256 al agent_id.

    Proprietatea cheie: fiecare agent_id distinct produce o fază distinctă în
    intervalul [0, jitter_ratio/2), garantând o distribuție naturală a timpilor
    de reconectare fără nicio coordonare centralizată.

    Implementare:
        - Se calculează SHA-256 al agent_id encodat UTF-8
        - Primii 4 bytes sunt interpretați ca întreg big-endian (0 … 2³²-1)
        - Se aplică modulo 10.000 pentru a obține un bucket în [0, 9999]
        - Rezultatul se normalizează la [0, jitter_ratio/2)

    Args:
        agent_id: Identificatorul unic al agentului EDR.
        jitter_ratio: Raportul maxim total de variație al delay-ului.

    Returns:
        Factor de jitter determinist în [0, jitter_ratio / 2).
    """
    digest = hashlib.sha256(agent_id.encode("utf-8")).digest()
    agent_int = int.from_bytes(digest[:4], byteorder="big")
    return (agent_int % 10_000) / 10_000.0 * (jitter_ratio / 2.0)


def compute_backoff_delay(
    consecutive_failures: int,
    agent_phase: float,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
    multiplier: float = DEFAULT_MULTIPLIER,
    jitter_ratio: float = DEFAULT_JITTER_RATIO,
) -> float:
    """
    Calculează delay-ul de așteptare înaintea următoarei tentative de reconectare.

    Implementează formula: W = min(W_max, W_base × multiplier^n) × (1 + φ + ε)

    Args:
        consecutive_failures: Numărul curent de eșecuri consecutive (0-indexat).
                              La 0 → delay = base_delay × 1 (fără multiplicare)
                              La 1 → delay = base_delay × 2
                              La 2 → delay = base_delay × 4
                              etc.
        agent_phase: Faza de jitter deterministă, calculată pentru agent.
        base_delay: Delay-ul de pornire în secunde (tipic = heartbeat_interval_seconds).
        max_delay: Plafonul maxim al delay-ului, indiferent de numărul de eșecuri.
        multiplier: Factorul de creștere exponențială.
        jitter_ratio: Variația maximă totală aplicată delay-ului calculat.

    Returns:
        Numărul de secunde de așteptare, cu jitter hibrid aplicat.
    """
    # Pasul 1: Backoff exponențial, plafonat la max_delay
    raw_delay = base_delay * (multiplier ** consecutive_failures)
    adjusted_max_delay = max_delay / (1.0 + jitter_ratio)
    capped_delay = min(adjusted_max_delay, raw_delay)

    # Pasul 2: Jitter hibrid (determinist + aleatoriu)
    random_component = random.uniform(0.0, jitter_ratio / 2.0)
    total_jitter = agent_phase + random_component

    return capped_delay * (1.0 + total_jitter)


# ---------------------------------------------------------------------------
# Starea mecanismului de backoff
# ---------------------------------------------------------------------------

@dataclass
class BackoffState:
    """
    Starea internă a mecanismului de reconnectare cu backoff exponențial.

    Separarea stării de logică permite inspecția externă a stării agentului
    și simplificarea testării unitare a controlerului.
    """

    consecutive_failures: int = 0
    total_failures: int = 0
    last_failure_time: Optional[float] = field(default=None)


# ---------------------------------------------------------------------------
# Controlerul principal
# ---------------------------------------------------------------------------

class HeartbeatBackoffController:
    """
    Controlează logica de retry cu backoff exponențial pentru heartbeat-urile agentului EDR.

    Responsabilități:
      - Urmărirea stării eșecurilor consecutive
      - Calculul delay-ului de așteptare adaptat prin compute_backoff_delay()
      - Logarea tranzițiilor de stare (degradare / recuperare)

    Garanții de comportament:
      - La primul eșec: delay ≈ base_delay (fără penalizare inițială excesivă)
      - La eșecuri repetate: delay crește exponențial până la max_delay
      - La recuperare: delay revine imediat la intervalul normal de heartbeat
      - Nicio cerință de coordonare între agenți: funcționează independent pe fiecare endpoint

    Exemplu de utilizare în heartbeat_loop:

        backoff = HeartbeatBackoffController(
            agent_id=config["agent_id"],
            base_delay=float(heartbeat_interval_seconds),
        )

        while True:
            try:
                send_event(server_url, payload)
                backoff.record_success()
                time.sleep(heartbeat_interval_seconds)
            except TransportError as error:
                logger.error("Heartbeat failed: %s", error)
                delay = backoff.record_failure()
                time.sleep(delay)
    """

    def __init__(
        self,
        agent_id: str,
        base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
        max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
        multiplier: float = DEFAULT_MULTIPLIER,
        jitter_ratio: float = DEFAULT_JITTER_RATIO,
        logger: Optional[logging.Logger] = None,
    ):
        self.agent_id = agent_id
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.multiplier = multiplier
        self.jitter_ratio = jitter_ratio
        self.logger = logger or logging.getLogger(__name__)
        self._state = BackoffState()
        self._agent_phase = _compute_agent_phase(agent_id, jitter_ratio)

    # ------------------------------------------------------------------
    # API public
    # ------------------------------------------------------------------

    def record_success(self) -> None:
        """
        Înregistrează un heartbeat reușit și resetează starea de backoff.

        Dacă agentul era în stare degradată (eșecuri anterioare), loghează
        explicit recuperarea conexiunii pentru vizibilitate operațională.
        """
        if self._state.consecutive_failures > 0:
            self.logger.info(
                "Server connection restored after %d consecutive failure(s). "
                "Resuming normal heartbeat interval.",
                self._state.consecutive_failures,
            )

        self._state.consecutive_failures = 0
        self._state.last_failure_time = None

    def record_failure(self) -> float:
        """
        Înregistrează un eșec de heartbeat și calculează delay-ul de așteptare.

        Delay-ul este calculat cu exponential backoff și jitter hibrid,
        începând de la base_delay la primul eșec.

        Returns:
            Numărul de secunde de așteptare înainte de reîncercare.
        """
        self._state.consecutive_failures += 1
        self._state.total_failures += 1
        self._state.last_failure_time = time.monotonic()

        # consecutive_failures - 1: la primul eșec (=1) folosim exponentul 0
        # astfel încât delay-ul inițial este exact base_delay (fără penalizare).
        delay = compute_backoff_delay(
            consecutive_failures=self._state.consecutive_failures - 1,
            agent_phase=self._agent_phase,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            multiplier=self.multiplier,
            jitter_ratio=self.jitter_ratio,
        )

        self.logger.warning(
            "Heartbeat failed (consecutive: %d | total: %d | next retry in: %.1fs).",
            self._state.consecutive_failures,
            self._state.total_failures,
            delay,
        )

        return delay

    # ------------------------------------------------------------------
    # Proprietăți de inspecție a stării
    # ------------------------------------------------------------------

    @property
    def consecutive_failures(self) -> int:
        """Numărul de eșecuri consecutive active."""
        return self._state.consecutive_failures

    @property
    def is_degraded(self) -> bool:
        """True dacă agentul este curent în stare degradată (server indisponibil)."""
        return self._state.consecutive_failures > 0