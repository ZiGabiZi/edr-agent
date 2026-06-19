import json
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse


REQUIRED_CONFIG_KEYS = ["agent_id", "server_url", "agent_version"]
CONFIG_PATH = Path("config.json")
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10


class ConfigError(Exception):
    """Eroare ridicată atunci când fișierul de configurare este invalid sau lipsește."""
    pass


def validate_config(config: Dict[str, Any]) -> None:
    """Validează prezența și tipul câmpurilor obligatorii."""
    if not isinstance(config, dict):
        raise ConfigError("Config file must contain a JSON object")

    missing_keys = []
    invalid_keys = []

    for field in REQUIRED_CONFIG_KEYS:
        if field not in config:
            missing_keys.append(field)
        elif not isinstance(config[field], str) or not config[field].strip():
            invalid_keys.append(field)

    if "heartbeat_interval_seconds" in config:
        heartbeat_interval = config["heartbeat_interval_seconds"]

        if (
            not isinstance(heartbeat_interval, int)
            or isinstance(heartbeat_interval, bool)
            or heartbeat_interval <= 0
        ):
            invalid_keys.append("heartbeat_interval_seconds")

    errors = []

    if missing_keys:
        errors.append(f"Missing required fields: {', '.join(missing_keys)}")

    if invalid_keys:
        errors.append(f"Invalid or empty values for fields: {', '.join(invalid_keys)}")

    if errors:
        raise ConfigError(" | ".join(errors))


def validate_server_url(server_url: str) -> None:
    """Validează formatul adresei serverului EDR."""
    parsed = urlparse(server_url)

    if parsed.scheme not in ("http", "https"):
        raise ConfigError("server_url must start with http:// or https://")

    if not parsed.netloc:
        raise ConfigError("server_url must contain a valid hostname")


def normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalizează configurația și adaugă valori implicite unde este cazul."""
    normalized_config = dict(config)

    normalized_config["agent_id"] = normalized_config["agent_id"].strip()
    normalized_config["server_url"] = normalized_config["server_url"].strip().rstrip("/")
    normalized_config["agent_version"] = normalized_config["agent_version"].strip()

    if "heartbeat_interval_seconds" not in normalized_config:
        normalized_config["heartbeat_interval_seconds"] = DEFAULT_HEARTBEAT_INTERVAL_SECONDS

    return normalized_config


def load_config(config_path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Citește, validează și normalizează configurația agentului."""
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except json.JSONDecodeError as error:
        raise ConfigError(f"Invalid JSON format in {config_path}: {error}") from error
    except OSError as error:
        raise ConfigError(f"Could not read config file {config_path}: {error}") from error

    validate_config(config)

    normalized_config = normalize_config(config)

    validate_server_url(normalized_config["server_url"])

    return normalized_config