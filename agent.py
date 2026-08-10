import logging
import signal
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Callable, Optional
from uuid import uuid4
from pathlib import Path

from services.event_dispatcher import EventDispatcher
from services.event_spool import EventSpool, EventSpoolError
from services.file_monitor import FileMonitor, FileMonitorError
from services.backoff import HeartbeatBackoffController
from services.config_loader import ConfigError, load_config
from services.stop_signal import StopSignal
from services.system_info import collect_system_info
from services.transport import (
    AgentNotRegisteredError,
    FatalTransportError,
    TransportError,
    check_server_health,
    register_agent,
    send_event,
    send_heartbeat,
)

_BASE_DIR = Path(__file__).resolve().parent
_LOG_FILE_PATH = _BASE_DIR / "agent.log"
_LOG_FILE_MAX_BYTES: int = 10 * 1024 * 1024
_LOG_FILE_BACKUP_COUNT: int = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[
        RotatingFileHandler(
            _LOG_FILE_PATH,
            maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parametri pentru faza de startup (mai agresivi — serverul trebuie găsit rapid)
# ---------------------------------------------------------------------------

_STARTUP_BASE_DELAY_SECONDS: float = 5.0
_STARTUP_MAX_DELAY_SECONDS: float = 60.0
_STARTUP_WARN_AFTER_RETRIES: int = 15
_PROCESS_INSTANCE_ID: str = str(uuid4())

def ensure_agent_instance_id(config: Dict[str, Any]) -> str:
    """
    Fixează în config incarnarea rulării curente și o returnează.

    Această funcție este singurul loc din agent care stabilește agent_instance_id.
    Toți builderii de payload doar *citesc* valoarea, niciodată nu o produc.

    Două proprietăți sunt obligatorii, iar ambele derivă din felul în care
    serverul interpretează câmpul:

      - stabilă pe durata unei rulări. Apelurile repetate întorc aceeași valoare,
        pentru că valoarea aparține procesului, nu apelului. Dacă fiecare apel ar
        genera un UUID nou, serverul ar vedea o repornire la fiecare heartbeat.

      - diferită între rulări. De aceea o valoare venită din config.json este
        ignorată deliberat: fiind fixă pe disc, ar fi identică la fiecare pornire,
        serverul n-ar mai observa nicio schimbare, iar detecția de repornire ar
        fi dezactivată permanent — exact eșecul tăcut pe care îl prevenim aici.
    """
    stale_instance_id = config.get("agent_instance_id")

    if stale_instance_id is not None and stale_instance_id != _PROCESS_INSTANCE_ID:
        logger.warning(
            "Ignoring agent_instance_id=%r found in configuration: the incarnation "
            "identifies the running process and is generated at startup. A fixed "
            "value on disk would be identical across runs and would permanently "
            "disable restart detection.",
            stale_instance_id,
        )

    config["agent_instance_id"] = _PROCESS_INSTANCE_ID
    return _PROCESS_INSTANCE_ID


def _require_agent_instance_id(config: Dict[str, Any]) -> str:
    """
    Citește incarnarea din config, refuzând absența ei.

    De ce eroare și nu None: o incarnare lipsă nu doar dezactivează detecția de
    repornire, ci transformă heartbeat-urile valide în date aruncate. Serverul
    sare complet peste ramura de repornire (`if instance_id is not None`) și
    evaluează doar secvența. Rularea nouă începe de la sequence=1, în timp ce pe
    server e memorat last_sequence de la rularea precedentă — de ordinul miilor
    după câteva ore. Fiecare heartbeat cade pe ramura `sequence < last_sequence`,
    e clasificat drept pachet reordonat și ignorat, fără ca last_sequence să
    avanseze. Agentul apare online (last_seen se actualizează necondiționat,
    înaintea oricărei ramificații), dar continuitatea nu mai e urmărită deloc
    până când contorul local ajunge din urmă valoarea de pe server.

    O excepție se vede la primul heartbeat. Tăcerea nu se vede niciodată.
    """
    instance_id = config.get("agent_instance_id")

    if not isinstance(instance_id, str) or not instance_id.strip():
        raise ValueError(
            f"agent_instance_id missing or empty in configuration "
            f"(got {instance_id!r}). The incarnation must be established with "
            f"ensure_agent_instance_id(config) before any payload is built."
        )

    return instance_id


# ---------------------------------------------------------------------------
# Funcții de construire a payload-urilor
# ---------------------------------------------------------------------------

def build_agent_registration_payload(
    config: Dict[str, Any],
    system_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Construiește payload-ul trimis către server pentru înregistrarea agentului."""
    return {
        "agent_id": config.get("agent_id", "unknown_agent"),
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
        "client_event_id": str(uuid4()),
        "agent_id": config.get("agent_id", "unknown_agent"),
        "agent_instance_id": _require_agent_instance_id(config),
        "event_type": "agent_startup",
        "occurred_at": current_time,
        "description": f"Agent started successfully at {current_time}",
    }


def build_shutdown_event_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    """Construiește evenimentul trimis la oprirea controlată a agentului."""
    current_time = datetime.now(timezone.utc).isoformat()

    return {
        "client_event_id": str(uuid4()),
        "agent_id": config.get("agent_id", "unknown_agent"),
        "agent_instance_id": _require_agent_instance_id(config),
        "event_type": "agent_shutdown",
        "occurred_at": current_time,
        "description": f"Agent stopped manually at {current_time}",
    }


def build_heartbeat_payload(config: Dict[str, Any], sequence: int) -> Dict[str, Any]:
    """
    Construiește payload-ul unui heartbeat.

    Numele cheilor trebuie să corespundă *exact* câmpurilor din HeartbeatRequest
    de pe server (app/schemas/heartbeat.py). Pydantic aruncă la validare cheile
    pe care schema nu le declară, așa că o cheie scrisă greșit nu oprește nimic:
    câmpul rămâne pur și simplu None pe server, iar logica ce depinde de el
    (detecția de repornire prin schimbarea incarnării) nu se declanșează
    niciodată. Serverul loghează acum cheia necunoscută (app/schemas/wire.py),
    dar abia după ce payload-ul greșit a fost deja trimis într-o rulare reală.
    De aceea perechea (builder, schemă) rămâne acoperită de test, care o prinde
    la commit — vezi tests/test_heartbeat_payload.py.
    Incarnarea este citită strict (_require_agent_instance_id): un config fără
    ea oprește construcția payload-ului în loc să emită None. Motivul detaliat
    al acestei stricteți e documentat în _require_agent_instance_id.
    """
    return {
        "agent_instance_id": _require_agent_instance_id(config),
        "sequence": sequence,
        "agent_version": config.get("agent_version"),
    }


def build_file_event_callback(
    spool: EventSpool,
    dispatcher: EventDispatcher,
) -> Callable[[Dict[str, Any]], None]:
    """
    Construiește callback-ul apelat de FileMonitor pentru fiecare eveniment de fișier.

    Callback-ul NU mai trimite pe rețea de pe thread-ul observer-ului watchdog:
    doar persistă evenimentul în coada locală și trezește dispatcher-ul.
    Consecințe:
      - un server picat sau un agent neînregistrat nu mai pierde evenimente —
        ele așteaptă pe disc până când livrarea redevine posibilă;
      - thread-ul observer-ului nu mai este blocat de timeout-uri HTTP (până
        la 5s per eveniment în implementarea anterioară).
    """
    def file_event_callback(event_payload: Dict[str, Any]) -> None:
        spool.enqueue(event_payload)
        dispatcher.wake()

    return file_event_callback


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

def register_agent_with_retry(
    config: Dict[str, Any],
    server_url: str,
    system_info: Dict[str, Any],
    stop_event: StopSignal,
    warn_after_retries: int = _STARTUP_WARN_AFTER_RETRIES,
) -> bool:
    """
    Încearcă repetat să contacteze serverul și să înregistreze agentul,
    folosind exponential backoff cu jitter hibrid.

    NU emite evenimentul agent_startup: acela este construit și pus în coada
    persistentă o singură dată per proces, în run_agent(). Funcția poate fi
    astfel apelată în siguranță și la reînregistrare (directiva 'reregister'
    sau HTTP 404 la heartbeat), fără a genera evenimente false de pornire.

    Parametrii de backoff pentru startup sunt deliberat mai agresivi decât cei
    din heartbeat_loop (bază 5s, plafon 60s vs. bază=interval, plafon=300s),
    deoarece înregistrarea este critică pentru funcționarea agentului, iar
    operatorul se poate afla în așteptare activă.

    Bucla NU abandonează niciodată din cauza numărului de încercări: un server
    inaccesibil este o stare tranzitorie normală (endpoint pornit înaintea
    serverului, rețea izolată, mentenanță), nu un motiv de oprire a agentului.
    Abandonul ar goli inutil endpoint-ul de monitorizare — run_agent() sare
    peste heartbeat_loop și intră direct în finally, oprind file monitor-ul,
    dispatcher-ul și spool-ul. Singurele ieșiri sunt FatalTransportError
    (configurare/autentificare greșită — reîncercarea nu poate ajuta) și
    setarea stop_event (oprire cerută explicit).

    Args:
        config: Configurația agentului.
        server_url: URL-ul serverului EDR.
        system_info: Informațiile despre sistem colectate la pornire.
        stop_event: Semnal de oprire (StopSignal) — funcția doar îl
            interoghează și așteaptă pe el; nu îl setează niciodată.
            Dacă este setat, bucla se încheie fără a mai reîncerca.
        warn_after_retries: Pragul de eșecuri consecutive după care se loghează
                    o singură dată un avertisment de posibilă configurare
                    greșită. NU limitează numărul de reîncercări.

    Returns:
        True dacă înregistrarea a reușit complet.
        False dacă oprirea a fost solicitată înainte de reușita înregistrării,
        sau dacă a apărut o eroare permanentă (FatalTransportError).
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

            backoff.record_success()
            return True
        
        except FatalTransportError as error:
            logger.critical(f"Permanent configuration or auth error detected: {error}")
            logger.critical("Aborting startup loop. Manual intervention required.")
            return False

        except TransportError as error:
            logger.error(f"Startup connection failed: {error}")
            delay = backoff.record_failure()
            if backoff.consecutive_failures == warn_after_retries:
                logger.warning(
                    f"Agent failed to register after {warn_after_retries} attempts. "
                    "Possible misconfiguration. Continuing startup loop."
                )
            stop_event.wait(timeout=delay)

    logger.info("Startup loop exited: stop was requested before registration completed.")
    return False


# ---------------------------------------------------------------------------
# Bucla principală de heartbeat
# ---------------------------------------------------------------------------

def heartbeat_loop(
    config: Dict[str, Any],
    server_url: str,
    system_info: Dict[str, Any],
    heartbeat_interval_seconds: float,
    stop_event: StopSignal,
) -> None:
    """
    Rulează bucla principală a agentului cu exponential backoff și sleep responsiv.

    Diferența față de implementarea anterioară cu time.sleep():
        stop_event.wait(timeout=N) are semantică identică cu time.sleep(N) în
        condiții normale, dar se trezește *imediat* dacă stop_event este setat
        (Ctrl+C sau SIGTERM). Agentul devine astfel responsiv la comenzi de
        oprire chiar și în mijlocul unui delay de 300 de secunde.

    Comportament în stare normală:
        Agentul trimite un heartbeat la fiecare heartbeat_interval_seconds
        (valoarea locală implicită, folosită doar până la primul răspuns).
        La succes, dacă serverul indică next_heartbeat_seconds în răspuns,
        acea valoare înlocuiește intervalul curent — serverul dictează astfel
        cadența întregului parc de agenți dintr-un singur loc. Dacă serverul
        nu trimite o valoare validă, agentul păstrează ultimul interval cunoscut.

    Comportament la eșec (server indisponibil):
        Eșec 1 → delay ≈ interval curent × 1  (ex: ~10s)
        Eșec 2 → delay ≈ interval curent × 2  (ex: ~20s)
        Eșec 3 → delay ≈ interval curent × 4  (ex: ~40s)
        ...până la plafonul maxim de 300 de secunde.
        La recuperarea conexiunii, intervalul revine imediat la valoarea normală.
    """
    logger.info(
        f"Starting heartbeat loop with interval={heartbeat_interval_seconds} seconds."
    )

    current_interval = heartbeat_interval_seconds

    # Contor de secvență per proces: pornește de la 1 la fiecare lansare a agentului
    # și crește monoton cu fiecare încercare de heartbeat. Serverul îl folosește ca
    # semnal de continuitate — un gol înseamnă heartbeat-uri pierdute, iar resetarea
    # la o valoare mai mică decât ultima cunoscută înseamnă că procesul agentului a
    # repornit (crash, kill, tampering) chiar dacă nu a apucat un shutdown controlat.
    heartbeat_sequence = 0

    backoff = HeartbeatBackoffController(
        agent_id=config["agent_id"],
        base_delay=float(current_interval),
        logger=logger,
    )

    while not stop_event.is_set():
        try:
            heartbeat_sequence += 1
            heartbeat_payload = build_heartbeat_payload(config, heartbeat_sequence)
            response = send_heartbeat(server_url, config["agent_id"], heartbeat_payload)
            logger.info(f"Heartbeat response: {response}")

            directive = response.get("directive") or {}
            action = directive.get("action", "none")

            if action == "reregister":
                logger.warning(
                    "Server requested re-registration of agent. Restarting startup loop..."
                )
                registered = register_agent_with_retry(config, server_url, system_info, stop_event)

                if not registered:
                    logger.info("Re-registration aborted due to stop request.")
                    break

            elif action == "update_ruleset":
                logger.info("Server requested ruleset update. Implement update logic here.")

            next_interval = response.get("next_heartbeat_seconds")
            if isinstance(next_interval, (int, float)) and next_interval > 0:
                if next_interval != current_interval:
                    logger.info(
                        f"Server adjusted heartbeat cadence: {current_interval}s -> {next_interval}s."
                    )
                current_interval = next_interval
                backoff.base_delay = float(current_interval)

            backoff.record_success()
            stop_event.wait(timeout=current_interval)

        except FatalTransportError as error:
            logger.critical(f"Permanent configuration or auth error detected: {error}")
            logger.critical("Aborting heartbeat loop. Manual intervention required.")
            return
        
        except AgentNotRegisteredError as error:
            logger.warning(
                "Server no longer recognizes this agent (%s). Re-registering...",
                error,
            )
            registered = register_agent_with_retry(config, server_url, system_info, stop_event)
            if not registered:
                logger.info("Re-registration failed or was aborted. Stopping heartbeat loop.")
                return

            backoff.record_success()
            logger.info(
                    "Re-registration succeeded. Resuming normal heartbeat cadence in %.1fs.",
                    current_interval,
                )
            stop_event.wait(timeout=current_interval)

        except TransportError as error:
            logger.error(f"Heartbeat transport error: {error}")
            delay = backoff.record_failure()
            stop_event.wait(timeout=delay)

def start_file_monitoring(
    config: Dict[str, Any],
    event_callback: Callable[[Dict[str, Any]], None],
) -> Optional[FileMonitor]:
    """
    Instanțiază și pornește FileMonitor pe baza configurației agentului.

    Dacă niciun director configurat nu este valid, monitorizarea nu poate porni,
    dar agentul continuă să funcționeze (heartbeat + evenimente de ciclu de viață).
    Returnează instanța pornită sau None dacă monitorizarea nu a putut porni.
    """
    monitor = FileMonitor(
        agent_id=config["agent_id"],
        monitored_directories=config["monitored_directories"],
        recursive_monitoring=config["recursive_monitoring"],
        event_callback=event_callback,
        logger=logger,
    )

    try:
        monitor.start()
        return monitor
    except FileMonitorError as error:
        logger.error(
            "File monitoring could not start: %s. "
            "Agent continues with heartbeat only.",
            error,
        )
        return None



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
    file_monitor = None
    event_spool = None
    event_dispatcher = None
    registered = False

    stop_event = threading.Event()

    def request_shutdown(signum=None, frame=None) -> None:
        logger.info("Shutdown signal received. Stopping agent gracefully...")
        stop_event.set()

    signal.signal(signal.SIGTERM, request_shutdown)

    logger.info("Starting endpoint agent...")
    logger.info("Press CTRL+C to stop the agent manually.")

    try:
        config = load_config()
        logger.info("Agent instance id (this run): %s", ensure_agent_instance_id(config))

        server_url = config["server_url"]
        heartbeat_interval_seconds = config["heartbeat_interval_seconds"]

        logger.info(f"Loaded configuration for agent_id={config['agent_id']}")
        logger.info(f"Server URL: {server_url}")
        logger.info(f"Heartbeat interval: {heartbeat_interval_seconds} seconds")

        system_info = collect_system_info(server_url)
        log_system_info(system_info)

        try:
            event_spool = EventSpool(logger=logger)
        except EventSpoolError as error:
            logger.error(
                "Event spool could not be opened: %s. "
                "File monitoring disabled; agent continues with heartbeat only.",
                error,
            )

        startup_event_payload = build_startup_event_payload(config)

        if event_spool is not None:
            event_dispatcher = EventDispatcher(
                spool=event_spool,
                server_url=server_url,
                agent_id=config["agent_id"],
                stop_event=stop_event,
                logger=logger,
            )
            event_spool.enqueue(startup_event_payload)
            event_dispatcher.start()
            file_monitor = start_file_monitoring(
                config,
                build_file_event_callback(event_spool, event_dispatcher),
            )

        registered = register_agent_with_retry(config, server_url, system_info, stop_event)

        if registered:
            if event_spool is None:
                try:
                    send_event(server_url, startup_event_payload)
                except TransportError as error:
                    logger.error(f"Could not send startup event directly: {error}")

            heartbeat_loop(
                config,
                server_url,
                system_info,
                heartbeat_interval_seconds,
                stop_event,
            )



    except KeyboardInterrupt:
        logger.info("Agent stopped manually by user (Ctrl+C).")
        stop_event.set()

    except ConfigError as error:
        logger.error(f"Configuration error: {error}")

    except Exception:
        logger.exception("Unexpected error occurred:")

    finally:
        stop_event.set()
        # Trimitem evenimentul de shutdown indiferent de cum s-a oprit agentul,
        # atât timp cât avem suficientă configurație și serverul poate fi accesibil.
        if file_monitor is not None and file_monitor.is_running():
            file_monitor.stop()
            file_monitor.join(timeout=5)

        if event_dispatcher is not None:
            event_dispatcher.stop()
            event_dispatcher.join(timeout=5)

        if event_spool is not None:
            event_spool.close()
            
        if config and registered and config.get("agent_id") and server_url is not None:
            try:
                shutdown_payload = build_shutdown_event_payload(config)
                shutdown_response = send_event(server_url, shutdown_payload)
                logger.info(f"Shutdown event response: {shutdown_response}")

            except TransportError as error:
                logger.error(f"Could not send shutdown event: {error}")

            except Exception as e:
                logger.exception(f"Failed to generate or send shutdown event due to internal error: {e}")

        logger.info("Agent stopped.")


if __name__ == "__main__":
    run_agent()