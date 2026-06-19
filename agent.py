import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

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


def heartbeat_loop(
    config: Dict[str, Any],
    server_url: str,
    heartbeat_interval_seconds: int,
) -> None:
    """
    Rulează bucla principală a agentului.

    Agentul trimite periodic evenimente de tip heartbeat către server.
    Dacă serverul nu răspunde temporar, agentul nu se oprește, ci loghează eroarea
    și încearcă din nou la următorul interval.
    """
    logger.info(
        f"Starting heartbeat loop with interval={heartbeat_interval_seconds} seconds."
    )
    logger.info("Press CTRL+C to stop the agent manually.")

    while True:
        try:
            heartbeat_payload = build_heartbeat_event_payload(config)
            heartbeat_response = send_event(server_url, heartbeat_payload)
            logger.info(f"Heartbeat sent successfully: {heartbeat_response}")

        except TransportError as error:
            logger.error(f"Heartbeat transport error: {error}")

        time.sleep(heartbeat_interval_seconds)


def run_agent() -> None:
    """Rulează agentul EDR minimal în mod long-running."""
    config = None
    server_url = None

    logger.info("Starting endpoint agent...")

    try:
        config = load_config()
        server_url = config["server_url"]
        heartbeat_interval_seconds = config["heartbeat_interval_seconds"]

        logger.info(f"Loaded configuration for agent_id={config['agent_id']}")
        logger.info(f"Server URL: {server_url}")
        logger.info(f"Heartbeat interval: {heartbeat_interval_seconds} seconds")

        system_info = collect_system_info(server_url)
        log_system_info(system_info)

        logger.info("Checking server health...")
        health_response = check_server_health(server_url)
        logger.info(f"Server health response: {health_response}")

        logger.info("Registering agent...")
        agent_payload = build_agent_registration_payload(config, system_info)
        register_response = register_agent(server_url, agent_payload)
        logger.info(f"Register response: {register_response}")

        logger.info("Sending startup event...")
        startup_event_payload = build_startup_event_payload(config)
        startup_event_response = send_event(server_url, startup_event_payload)
        logger.info(f"Startup event response: {startup_event_response}")

        heartbeat_loop(config, server_url, heartbeat_interval_seconds)

    except KeyboardInterrupt:
        logger.info("Agent stopped manually by user.")

        if config is not None and server_url is not None:
            try:
                shutdown_payload = build_shutdown_event_payload(config)
                shutdown_response = send_event(server_url, shutdown_payload)
                logger.info(f"Shutdown event response: {shutdown_response}")
            except TransportError as error:
                logger.error(f"Could not send shutdown event: {error}")

    except ConfigError as error:
        logger.error(f"Configuration error: {error}")

    except TransportError as error:
        logger.error(f"Transport error: {error}")

    except Exception:
        logger.exception("Unexpected error occurred:")


if __name__ == "__main__":
    run_agent()