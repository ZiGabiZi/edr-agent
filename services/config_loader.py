import json
import os
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse


REQUIRED_CONFIG_KEYS = ["agent_id", "server_url", "agent_version"]
CONFIG_PATH = Path("config.json")
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10
DEFAULT_MONITORED_DIRECTORIES = [r"C:\EDR_Test"]
DEFAULT_RECURSIVE_MONITORING = True


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

    if "recursive_monitoring" in config:
        recursive_monitoring = config["recursive_monitoring"]

        if not isinstance(recursive_monitoring, bool):
            invalid_keys.append("recursive_monitoring")

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

    if not parsed.hostname:
        raise ConfigError("server_url must contain a valid hostname")
    
    try:
        _ = parsed.port
    except ValueError as error:
        raise ConfigError(f"Invalid port in server_url: {error}") from error

def validate_monitored_directories(directories: Any) -> List[str]:
    if not isinstance(directories, list) or not directories:
        raise ConfigError("monitored_directories must be a non-empty list of directory paths")
    valid_directories = []

    for index, directory in enumerate(directories):
        if not isinstance(directory, str) or not directory.strip():
            raise ConfigError(f"monitored_directories[{index}] must be a non-empty string")
        
        normalized_directory = os.path.normpath(
            os.path.expanduser(directory.strip())
        )

        if not Path(normalized_directory).is_absolute():
            raise ConfigError(
                f"monitored_directories[{index}] must be an absolute path: "
                f"{directory}"
                )
        valid_directories.append(normalized_directory)

    return valid_directories


def normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalizează configurația și adaugă valori implicite unde este cazul."""
    normalized_config = dict(config)

    normalized_config["agent_id"] = normalized_config["agent_id"].strip()
    normalized_config["server_url"] = normalized_config["server_url"].strip().rstrip("/")
    normalized_config["agent_version"] = normalized_config["agent_version"].strip()

    if "heartbeat_interval_seconds" not in normalized_config:
        normalized_config["heartbeat_interval_seconds"] = DEFAULT_HEARTBEAT_INTERVAL_SECONDS

    if "monitored_directories" not in normalized_config:
        normalized_config["monitored_directories"] = list(DEFAULT_MONITORED_DIRECTORIES)

    normalized_config["monitored_directories"] = validate_monitored_directories(
        normalized_config["monitored_directories"]
        )    

    if "recursive_monitoring" not in normalized_config:
        normalized_config["recursive_monitoring"] = DEFAULT_RECURSIVE_MONITORING

    unique_directories = []
    seen_directories = set()

    for directory in normalized_config["monitored_directories"]:
        directory_key = os.path.normcase(directory)

        if directory_key not in seen_directories:
            unique_directories.append(directory)
            seen_directories.add(directory_key)

    normalized_config["monitored_directories"] = unique_directories

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