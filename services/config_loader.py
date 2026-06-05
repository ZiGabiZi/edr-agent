import json
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse


REQUIRED_CONFIG_KEYS = ["agent_id", "server_url", "agent_version"]
CONFIG_PATH = Path("config.json")


class ConfigError(Exception):
    """Eroare ridicată atunci când fișierul de configurare este invalid sau lipsește."""
    pass


def validate_config(config: Dict[str, Any]) -> None:
    """Validează prezența și tipul câmpurilor obligatorii."""
    missing_keys = []
    invalid_keys = []

    for field in REQUIRED_CONFIG_KEYS:
        if field not in config:
            missing_keys.append(field)
        elif not isinstance(config[field], str) or not config[field].strip():
            invalid_keys.append(field)

    errors = []
    if missing_keys:
        errors.append(f"Missing required fields: {', '.join(missing_keys)}")
    if invalid_keys:
        errors.append(f"Invalid or empty values for fields: {', '.join(invalid_keys)}")

    if errors:
        raise ConfigError(" | ".join(errors))

def validate_server_url(server_url: str) -> None:
    parsed = urlparse(server_url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError("server_url must be a valid HTTP or HTTPS URL")
    if not parsed.netloc:
        raise ConfigError("server_url must contain a valid hostname")

def load_config(config_path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Citește, validează și normalizează configurația agentului."""
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except json.JSONDecodeError as error:
        raise ConfigError(f"Invalid JSON format in {config_path}: {error}") from error

    validate_config(config)

    # Normalizare date post-validare
    validate_server_url(config["server_url"])
    config["server_url"] = config["server_url"].rstrip("/")

    return config