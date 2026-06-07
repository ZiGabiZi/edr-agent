import hashlib
import platform
import socket
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse


def get_hostname() -> str:
    """Returnează numele endpointului."""
    return socket.gethostname()


def get_operating_system() -> str:
    """Returnează informații despre sistemul de operare."""
    system_name = platform.system()
    system_release = platform.release()
    system_version = platform.version()

    return f"{system_name} {system_release} ({system_version})"


def get_architecture() -> str:
    """Returnează arhitectura hardware raportată de sistem."""
    return platform.machine()


def get_os_architecture() -> str:
    """Returnează dacă sistemul de operare este pe 32-bit sau 64-bit."""
    return platform.architecture()[0]


def get_windows_machine_guid() -> Optional[str]:
    """Extrage MachineGuid din Windows Registry."""
    if platform.system().lower() != "windows":
        return None

    try:
        import winreg

        registry_path = r"SOFTWARE\Microsoft\Cryptography"

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, registry_path) as key:
            machine_guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(machine_guid).strip()
    except OSError:
        return None


def get_linux_machine_id() -> Optional[str]:
    """Extrage machine-id pe Linux."""
    if platform.system().lower() != "linux":
        return None

    possible_paths = [
        Path("/etc/machine-id"),
        Path("/var/lib/dbus/machine-id"),
    ]

    for path in possible_paths:
        try:
            if path.exists():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            continue

    return None


def get_mac_address() -> Optional[str]:
    """Returnează adresa MAC ca fallback pentru identificare."""
    try:
        mac_int = uuid.getnode()
        mac = ":".join(f"{(mac_int >> shift) & 0xff:02x}" for shift in range(40, -1, -8))
        return mac
    except Exception:
        return None


def get_stable_machine_identifier() -> Tuple[str, Optional[str]]:
    """
    Returnează un identificator stabil al mașinii și tipul acestuia.

    Ordinea preferată:
    1. Windows MachineGuid
    2. Linux /etc/machine-id
    3. MAC address fallback
    """

    windows_guid = get_windows_machine_guid()
    if windows_guid:
        return "windows_machine_guid", windows_guid

    linux_machine_id = get_linux_machine_id()
    if linux_machine_id:
        return "linux_machine_id", linux_machine_id

    mac_address = get_mac_address()
    if mac_address:
        return "mac_address_fallback", mac_address

    return "unknown", None


def hash_identifier(identifier: Optional[str]) -> Optional[str]:
    """Calculează SHA-256 pentru identificatorul stabil."""
    if not identifier:
        return None

    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()


def get_local_ip(server_url: Optional[str] = None) -> Optional[str]:
    """
    Încearcă să determine adresa IP locală folosită pentru comunicarea cu serverul.

    În loc să folosim 8.8.8.8, folosim adresa serverului EDR.
    Astfel, metoda funcționează mai bine în rețele host-only sau air-gapped.
    """

    target_host = None
    target_port = 80

    if server_url:
        parsed_url = urlparse(server_url)
        target_host = parsed_url.hostname
        target_port = parsed_url.port or 80

    if target_host:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target_host, target_port))
                return sock.getsockname()[0]
        except OSError:
            pass

    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return None


def collect_system_info(server_url: Optional[str] = None) -> Dict[str, Optional[str]]:
    """
    Colectează informații de bază despre endpoint.

    Aceste informații sunt folosite la înregistrarea agentului pe server.
    IP-ul și hostname-ul sunt metadate volatile.
    machine_id_hash este folosit pentru identitate persistentă.
    """

    machine_id_type, machine_identifier = get_stable_machine_identifier()

    return {
        "hostname": get_hostname(),
        "operating_system": get_operating_system(),
        "architecture": get_architecture(),
        "os_architecture": get_os_architecture(),
        "ip_address": get_local_ip(server_url),
        "machine_id_type": machine_id_type,
        "machine_id_hash": hash_identifier(machine_identifier),
    }


# Mici ajustari viitor:
# 1)Case pentru sistemul de operare MacOS
# 2)Fragilitatea adresei MAC ca identificator stabil
# 2.1)un fallback pentru a evita problemele în cazul în care adresa MAC nu este disponibilă sau este aceeași pe mai multe mașini (ex: în cazul mașinilor virtuale).