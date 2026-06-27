import logging
import signal
import threading
from datetime import datetime, timezone
from typing import Any, Dict

from services.backoff import HeartbeatBackoffController
from services.config_loader import ConfigError, load_config
from services.system_info import collect_system_info
from services.transport import (
    TransportError,
    check_server_health,
    register_agent,
    send_event,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler("agent.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parametri pentru faza de startup (mai agresivi — serverul trebuie găsit rapid)
# ---------------------------------------------------------------------------

_STARTUP_BASE_DELAY_SECONDS: float = 5.0
_STARTUP_MAX_DELAY_SECONDS: float = 60.0


# ---------------------------------------------------------------------------
# Funcții de construire a payload-urilor (nemodificate)
# ---------------------------------------------------------------------------

def build_agent_registration_payload(
    config: Dict[str, Any],
    system_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Construiește payload-ul trimis către server pentru înregistrarea agentului."""
    return {
        "agent_id": config["agent_id"],
        "hostname": system_info.get("hostname"),
        "operating_system": system_info.get("operating_system"),
        "ip_address": system_info.get("ip_address"),
        "agent_version": config.get("agent_version"),
        "machine_id_type": system_info.get("machine_id_type"),
        "machine_id_hash": system_info.get("machine_id_hash"),
        "architecture": system_info.get("architecture"),
        "os_architecture": system_info.get("os_architecture"),
    }


def build_startup_event_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    """Construiește evenimentul inițial trimis de agent după pornire."""
    current_time = datetime.now(timezone.utc).isoformat()
    
    return {
        "agent_id": config["agent_id"],
        "event_type": "agent_startup",
        "description": f"Agent started successfully at {current_time}",
    }


def build_heartbeat_event_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    """Construiește evenimentul periodic de heartbeat."""
    current_time = datetime.now(timezone.utc).isoformat()

    return {
        "agent_id": config["agent_id"],
        "event_type": "heartbeat",
        "description": f"Agent heartbeat at {current_time}",
    }


def build_shutdown_event_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    """Construiește evenimentul trimis la oprirea controlată a agentului."""
    current_time = datetime.now(timezone.utc).isoformat()

    return {
        "agent_id": config["agent_id"],
        "event_type": "agent_shutdown",
        "description": f"Agent stopped manually at {current_time}",
    }


def log_system_info(system_info: Dict[str, Any]) -> None:
    """Înregistrează în log informațiile colectate despre endpoint."""
    logger.info("Collected system information:")
    logger.info(f"  Hostname: {system_info.get('hostname')}")
    logger.info(f"  Operating system: {system_info.get('operating_system')}")
    logger.info(f"  IP address: {system_info.get('ip_address')}")
    logger.info(f"  Architecture: {system_info.get('architecture')}")
    logger.info(f"  OS architecture: {system_info.get('os_architecture')}")
    logger.info(f"  Machine ID type: {system_info.get('machine_id_type')}")
    logger.info(f"  Machine ID hash: {system_info.get('machine_id_hash')}")


# ---------------------------------------------------------------------------
# Faza de startup — rezistentă la serverul picat la pornirea agentului
# ---------------------------------------------------------------------------

def startup_loop(
    config: Dict[str, Any],
    server_url: str,
    system_info: Dict[str, Any],
    stop_event: threading.Event,
) -> bool:
    """
    Încearcă repetat să contacteze serverul, să înregistreze agentul și să trimită
    evenimentul de startup, folosind exponential backoff cu jitter hibrid.

    Această funcție rezolvă scenariul în care agentul pornește înainte ca serverul
    EDR să fie disponibil — situație frecventă la repornirea infrastructurii sau
    la pornirea automată a agentului ca serviciu de sistem.

    Parametrii de backoff pentru startup sunt deliberat mai agresivi decât cei
    din heartbeat_loop (bază 5s, plafon 60s vs. bază=interval, plafon=300s),
    deoarece înregistrarea este critică pentru funcționarea agentului, iar
    operatorul se poate afla în așteptare activă.

    Args:
        config: Configurația agentului.
        server_url: URL-ul serverului EDR.
        system_info: Informațiile despre sistem colectate la pornire.
        stop_event: Eveniment de oprire — dacă este setat, funcția se oprește
                    imediat fără a mai reîncerca.

    Returns:
        True dacă înregistrarea a reușit complet.
        False dacă oprirea a fost solicitată înainte de reușita înregistrării.
    """
    logger.info(
        "Attempting to connect to EDR server at %s (startup backoff: base=%.0fs, max=%.0fs)...",
        server_url,
        _STARTUP_BASE_DELAY_SECONDS,
        _STARTUP_MAX_DELAY_SECONDS,
    )

    backoff = HeartbeatBackoffController(
        agent_id=config["agent_id"],
        base_delay=_STARTUP_BASE_DELAY_SECONDS,
        max_delay=_STARTUP_MAX_DELAY_SECONDS,
        logger=logger,
    )

    while not stop_event.is_set():
        try:
            health_response = check_server_health(server_url)
            logger.info(f"Server health response: {health_response}")

            agent_payload = build_agent_registration_payload(config, system_info)
            register_response = register_agent(server_url, agent_payload)
            logger.info(f"Register response: {register_response}")

            startup_payload = build_startup_event_payload(config)
            startup_response = send_event(server_url, startup_payload)
            logger.info(f"Startup event response: {startup_response}")

            backoff.record_success()
            return True

        except TransportError as error:
            logger.error(f"Startup connection failed: {error}")
            delay = backoff.record_failure()
            # wait() se trezește imediat dacă stop_event este setat —
            # spre deosebire de time.sleep() care ar bloca pentru întreaga durată.
            stop_event.wait(timeout=delay)

    logger.info("Startup loop exited: stop was requested before registration completed.")
    return False


# ---------------------------------------------------------------------------
# Bucla principală de heartbeat
# ---------------------------------------------------------------------------

def heartbeat_loop(
    config: Dict[str, Any],
    server_url: str,
    heartbeat_interval_seconds: int,
    stop_event: threading.Event,
) -> None:
    """
    Rulează bucla principală a agentului cu exponential backoff și sleep responsiv.

    Diferența față de implementarea anterioară cu time.sleep():
        stop_event.wait(timeout=N) are semantică identică cu time.sleep(N) în
        condiții normale, dar se trezește *imediat* dacă stop_event este setat
        (Ctrl+C sau SIGTERM). Agentul devine astfel responsiv la comenzi de
        oprire chiar și în mijlocul unui delay de 300 de secunde.

    Comportament în stare normală:
        Agentul trimite un heartbeat la fiecare heartbeat_interval_seconds.
        La succes, intervalul rămâne constant.

    Comportament la eșec (server indisponibil):
        Eșec 1 → delay ≈ heartbeat_interval × 1  (ex: ~10s)
        Eșec 2 → delay ≈ heartbeat_interval × 2  (ex: ~20s)
        Eșec 3 → delay ≈ heartbeat_interval × 4  (ex: ~40s)
        ...până la plafonul maxim de 300 de secunde.
        La recuperarea conexiunii, intervalul revine imediat la valoarea normală.
    """
    logger.info(
        f"Starting heartbeat loop with interval={heartbeat_interval_seconds} seconds."
    )

    backoff = HeartbeatBackoffController(
        agent_id=config["agent_id"],
        base_delay=float(heartbeat_interval_seconds),
        logger=logger,
    )

    while not stop_event.is_set():
        try:
            heartbeat_payload = build_heartbeat_event_payload(config)
            heartbeat_response = send_event(server_url, heartbeat_payload)
            logger.info(f"Heartbeat sent successfully: {heartbeat_response}")

            backoff.record_success()
            stop_event.wait(timeout=heartbeat_interval_seconds)

        except TransportError as error:
            logger.error(f"Heartbeat transport error: {error}")
            delay = backoff.record_failure()
            stop_event.wait(timeout=delay)


# ---------------------------------------------------------------------------
# Orchestratorul principal
# ---------------------------------------------------------------------------

def run_agent() -> None:
    """
    Rulează agentul EDR în mod long-running.

    Gestionează ciclul complet de viață:
        1. Încărcarea configurației
        2. Colectarea informațiilor despre sistem
        3. Startup cu backoff (rezistent la serverul picat la pornire)
        4. Bucla de heartbeat cu backoff și sleep responsiv
        5. Shutdown controlat cu trimiterea evenimentului de oprire

    Semnale de oprire acceptate:
        - Ctrl+C (SIGINT / KeyboardInterrupt) — oprire manuală din consolă
        - SIGTERM — oprire prin serviciu de sistem (systemd, Task Scheduler)
          Notă: pe Windows, SIGTERM are suport limitat în afara mediilor POSIX.
          Pentru instalarea ca serviciu Windows nativ, se recomandă integrarea
          cu win32serviceutil.ServiceFramework.
    """
    config = None
    server_url = None

    stop_event = threading.Event()

    def request_shutdown(signum=None, frame=None) -> None:
        logger.info("Shutdown signal received. Stopping agent gracefully...")
        stop_event.set()

    signal.signal(signal.SIGTERM, request_shutdown)

    logger.info("Starting endpoint agent...")
    logger.info("Press CTRL+C to stop the agent manually.")

    try:
        config = load_config()
        server_url = config["server_url"]
        heartbeat_interval_seconds = config["heartbeat_interval_seconds"]

        logger.info(f"Loaded configuration for agent_id={config['agent_id']}")
        logger.info(f"Server URL: {server_url}")
        logger.info(f"Heartbeat interval: {heartbeat_interval_seconds} seconds")

        system_info = collect_system_info(server_url)
        log_system_info(system_info)

        registered = startup_loop(config, server_url, system_info, stop_event)

        if registered:
            heartbeat_loop(config, server_url, heartbeat_interval_seconds, stop_event)

    except KeyboardInterrupt:
        logger.info("Agent stopped manually by user (Ctrl+C).")
        stop_event.set()

    except ConfigError as error:
        logger.error(f"Configuration error: {error}")

    except Exception:
        logger.exception("Unexpected error occurred:")

    finally:
        # Trimitem evenimentul de shutdown indiferent de cum s-a oprit agentul,
        # atât timp cât avem suficientă configurație și serverul poate fi accesibil.
        if config is not None and server_url is not None:
            try:
                shutdown_payload = build_shutdown_event_payload(config)
                shutdown_response = send_event(server_url, shutdown_payload)
                logger.info(f"Shutdown event response: {shutdown_response}")
            except TransportError as error:
                logger.error(f"Could not send shutdown event: {error}")

        logger.info("Agent stopped.")


if __name__ == "__main__":
    run_agent()