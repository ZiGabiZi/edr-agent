# Agentul minimal va
# - citi config.json
# - verifica serverul EDR
# - colecta date despre endpoint
# - se inregistreaza la server
# - trimite events de pornire
import logging
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
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("EDRAgent")

def build_agent_registration_payload(
    config: Dict[str, Any],
    system_info: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Construiește payload-ul trimis către server pentru înregistrarea agentului.

    Ruta folosită:
    POST /api/agents/register
    """

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
    """
    Construiește evenimentul inițial trimis de agent după pornire.

    Ruta folosită:
    POST /api/events
    """

    current_time = datetime.now(timezone.utc).isoformat()

    return {
        "agent_id": config["agent_id"],
        "event_type": "agent_startup",
        "description": f"Agent started successfully at {current_time}",
    }


def  log_system_info(system_info: Dict[str, Any]) -> None:
    """
    Afișează informațiile colectate despre endpoint.
    """

    logger.info("Collected system information:")
    logger.info(f"  Hostname: {system_info.get('hostname')}")
    logger.info(f"  Operating system: {system_info.get('operating_system')}")
    logger.info(f"  IP address: {system_info.get('ip_address')}")
    logger.info(f"  Architecture: {system_info.get('architecture')}")
    logger.info(f"  OS architecture: {system_info.get('os_architecture')}")
    logger.info(f"  Machine ID type: {system_info.get('machine_id_type')}")
    logger.info(f"  Machine ID hash: {system_info.get('machine_id_hash')}")


def run_agent() -> None:
    """
    Rulează fluxul minimal al agentului EDR.

    Pași:
    1. citește configurația locală;
    2. colectează informații despre endpoint;
    3. verifică disponibilitatea serverului;
    4. înregistrează agentul la server;
    5. trimite evenimentul inițial agent_startup.
    """

    logger.info("Starting minimal endpoint agent...")

    try:
        config = load_config()
        server_url = config["server_url"]

        logger.info(f"Loaded configuration for agent_id={config['agent_id']}")
        logger.info(f"Server URL: {server_url}")

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
        event_payload = build_startup_event_payload(config)
        event_response = send_event(server_url, event_payload)
        logger.info(f"Event response: {event_response}")

        logger.info("Minimal agent flow completed successfully.")

    except ConfigError as error:
        logger.error(f"Configuration error: {error}")

    except TransportError as error:
        logger.error(f"Transport error: {error}")

    except Exception as error:
        logger.exception("Unexpected error occurred:")


if __name__ == "__main__":
    run_agent()