"""
Teste pentru reglajele fluxului de fișiere, din config.json până în hasher.
===========================================================================

Reglajele astea nu sunt confort de operator: pragul de dimensiune peste care un
fișier nu se mai citește deloc e chiar parametrul pe care măsurătoarea
octeți-divulgați-vs-always-upload îl variază. Ca constantă de modul, singurul
mod de a-l schimba era editarea codului agentului între rulări.

Ce se verifică aici, în ordinea în care lucrurile pot să tacă:

  1. forma valorilor — un "10" cu ghilimele sau un true din JSON trebuie
     respins la încărcare, nu descoperit într-o buclă de timp;
  2. traseul — o cheie poate exista în config, poate fi validată, și totuși
     nimeni să n-o citească. Asta e clasa de bug pe care un reglaj nou o aduce
     cel mai des, și e complet tăcută: agentul pornește, pare configurat, și
     rulează pe valoarea implicită;
  3. absența — o cheie lipsă trebuie să lase implicitul modulului în pace, NU
     să devină None sau o copie a valorii implicite ținută în alt fișier.
"""

import unittest
from typing import Any, Dict

import agent
from services.config_loader import FILE_PIPELINE_TUNABLES, ConfigError, validate_config
from services.file_hasher import (
    DEFAULT_SHUTDOWN_HASH_BUDGET_SECONDS,
    MAX_HASH_QUEUE_DEPTH,
    MAX_HASH_REINTRODUCTIONS,
    MAX_HASHABLE_FILE_SIZE_BYTES,
)
from services.file_monitor import FileMonitor
from services.settle_tracker import (
    DEFAULT_MAX_SETTLE_WAIT_SECONDS,
    DEFAULT_SETTLE_QUIET_SECONDS,
)


def _base_config(**overrides: Any) -> Dict[str, Any]:
    config = {
        "agent_id": "endpoint-01",
        "server_url": "http://127.0.0.1:8000",
        "agent_version": "0.1.0",
    }
    config.update(overrides)
    return config


class ValidationTests(unittest.TestCase):
    """Forma valorilor, verificată la încărcare."""

    def test_every_tunable_is_accepted_when_well_formed(self) -> None:
        config = _base_config(
            settle_quiet_seconds=2.5,
            settle_max_wait_seconds=120,
            release_poll_seconds=0.5,
            shutdown_report_reserve_seconds=2,
            shutdown_hash_budget_seconds=10.0,
            hash_max_file_size_bytes=50 * 1024 * 1024,
            hash_queue_depth=100,
            hash_max_reintroductions=5,
        )

        validate_config(config)  # nu trebuie să ridice nimic

    def test_a_missing_tunable_is_not_an_error(self) -> None:
        validate_config(_base_config())

    def test_a_boolean_is_rejected_for_every_tunable(self) -> None:
        """
        În Python bool e subclasă de int, deci un `true` din JSON ar trece
        drept 1: o cadență de o secundă, o coadă de un fișier, un prag de un
        octet. Aceeași greșeală a fost deja reparată o dată pentru
        heartbeat_interval_seconds; garda de aici o oprește pentru toate
        reglajele deodată, inclusiv cele care nu există încă.
        """
        for key in FILE_PIPELINE_TUNABLES:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError) as raised:
                    validate_config(_base_config(**{key: True}))

                self.assertIn(key, str(raised.exception))

    def test_a_string_is_rejected_for_every_tunable(self) -> None:
        for key in FILE_PIPELINE_TUNABLES:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    validate_config(_base_config(**{key: "10"}))

    def test_a_negative_value_is_rejected_for_every_tunable(self) -> None:
        for key in FILE_PIPELINE_TUNABLES:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    validate_config(_base_config(**{key: -1}))

    def test_a_float_is_rejected_where_the_value_counts_things(self) -> None:
        """Octeți și numărători sunt întregi; secundele pot fi fracționare."""
        for key in ("hash_max_file_size_bytes", "hash_queue_depth",
                    "hash_max_reintroductions"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    validate_config(_base_config(**{key: 1.5}))

    def test_zero_is_allowed_where_zero_has_a_meaning(self) -> None:
        """
        Zero nu e uniform greșit. Un buget de hashing zero la oprire înseamnă
        „raportează tot fără hash", zero reintroduceri înseamnă „nu reîncerca",
        iar o coadă de zero înseamnă „nu hash-ui nimic sub presiune" — toate
        sunt configurații de experiment legitime.
        """
        for key in (
            "shutdown_hash_budget_seconds",
            "shutdown_report_reserve_seconds",
            "hash_max_reintroductions",
            "hash_queue_depth",
            "hash_max_file_size_bytes",
            "settle_quiet_seconds",
            "settle_max_wait_seconds",
        ):
            with self.subTest(key=key):
                validate_config(_base_config(**{key: 0}))

    def test_a_zero_poll_interval_is_rejected(self) -> None:
        """Singura excepție: un poll de zero e busy-spin, nu reglaj."""
        with self.assertRaises(ConfigError):
            validate_config(_base_config(release_poll_seconds=0))


class WiringTests(unittest.TestCase):
    """
    Traseul de la config până la piesa care consumă reglajul.

    Clasa asta e motivul pentru care fișierul există: o cheie validată pe care
    nimeni n-o citește e complet tăcută — agentul pornește, pare configurat, și
    rulează pe implicit.
    """

    def _monitor(self, **kwargs) -> FileMonitor:
        return FileMonitor(
            agent_id="agent-test",
            agent_instance_id="incarnation-test",
            monitored_directories=[],
            recursive_monitoring=False,
            event_callback=lambda payload: None,
            **kwargs,
        )

    def test_the_monitor_forwards_every_tunable_to_the_piece_that_uses_it(self) -> None:
        monitor = self._monitor(
            quiet_seconds=2.5,
            max_wait_seconds=120.0,
            report_reserve_seconds=2.0,
            max_file_size_bytes=1234,
            max_queue_depth=7,
            max_reintroductions=9,
            shutdown_budget_seconds=11.0,
        )

        self.assertEqual(monitor.tracker.quiet_seconds, 2.5)
        self.assertEqual(monitor.tracker.max_wait_seconds, 120.0)
        self.assertEqual(monitor.report_reserve_seconds, 2.0)
        self.assertEqual(monitor.hasher.max_file_size_bytes, 1234)
        self.assertEqual(monitor.hasher.max_queue_depth, 7)
        self.assertEqual(monitor.hasher.max_reintroductions, 9)
        self.assertEqual(monitor.hasher.shutdown_budget_seconds, 11.0)

    def test_an_unconfigured_monitor_keeps_the_module_defaults(self) -> None:
        """
        Valorile implicite trăiesc lângă codul care le folosește. Testul le
        citește de acolo, nu le rescrie: o copie aici ar fi exact a doua sursă
        de adevăr pe care tot mecanismul o evită.
        """
        monitor = self._monitor()

        self.assertEqual(monitor.tracker.quiet_seconds, DEFAULT_SETTLE_QUIET_SECONDS)
        self.assertEqual(
            monitor.tracker.max_wait_seconds, DEFAULT_MAX_SETTLE_WAIT_SECONDS
        )
        self.assertEqual(
            monitor.hasher.max_file_size_bytes, MAX_HASHABLE_FILE_SIZE_BYTES
        )
        self.assertEqual(monitor.hasher.max_queue_depth, MAX_HASH_QUEUE_DEPTH)
        self.assertEqual(
            monitor.hasher.max_reintroductions, MAX_HASH_REINTRODUCTIONS
        )
        self.assertEqual(
            monitor.hasher.shutdown_budget_seconds,
            DEFAULT_SHUTDOWN_HASH_BUDGET_SECONDS,
        )


class ConfigToMonitorTests(unittest.TestCase):
    """Ultima verigă: start_file_monitoring chiar citește cheile din config."""

    def setUp(self) -> None:
        self.captured: Dict[str, Any] = {}
        real_monitor = agent.FileMonitor

        def capturing_monitor(**kwargs):
            self.captured = kwargs
            monitor = real_monitor(**kwargs)
            monitor.start = lambda: None  # type: ignore[method-assign]
            return monitor

        agent.FileMonitor = capturing_monitor  # type: ignore[assignment]
        self.addCleanup(setattr, agent, "FileMonitor", real_monitor)

    def _start(self, **tunables) -> None:
        config = {
            "agent_id": "endpoint-01",
            "agent_instance_id": "11111111-2222-3333-4444-555555555555",
            "monitored_directories": [],
            "recursive_monitoring": False,
        }
        config.update(tunables)
        agent.start_file_monitoring(config, lambda payload: None)

    def test_every_configured_tunable_reaches_the_monitor(self) -> None:
        self._start(
            settle_quiet_seconds=2.5,
            settle_max_wait_seconds=120,
            release_poll_seconds=0.5,
            shutdown_report_reserve_seconds=2,
            shutdown_hash_budget_seconds=10.0,
            hash_max_file_size_bytes=1234,
            hash_queue_depth=7,
            hash_max_reintroductions=9,
        )

        self.assertEqual(self.captured["quiet_seconds"], 2.5)
        self.assertEqual(self.captured["max_wait_seconds"], 120)
        self.assertEqual(self.captured["release_poll_seconds"], 0.5)
        self.assertEqual(self.captured["report_reserve_seconds"], 2)
        self.assertEqual(self.captured["shutdown_budget_seconds"], 10.0)
        self.assertEqual(self.captured["max_file_size_bytes"], 1234)
        self.assertEqual(self.captured["max_queue_depth"], 7)
        self.assertEqual(self.captured["max_reintroductions"], 9)

    def test_an_absent_key_is_omitted_rather_than_passed_as_none(self) -> None:
        """
        Diferența contează: un None pasat ar suprascrie implicitul modulului cu
        nimic, iar reglajul ar deveni brusc invalid la prima folosire. Cheia
        absentă trebuie să lipsească din apel.
        """
        self._start(hash_queue_depth=7)

        self.assertEqual(self.captured["max_queue_depth"], 7)

        for parameter in agent.FILE_PIPELINE_CONFIG_TO_PARAMETER.values():
            if parameter == "max_queue_depth":
                continue

            with self.subTest(parameter=parameter):
                self.assertNotIn(parameter, self.captured)


class TableSyncTests(unittest.TestCase):
    """
    Cele două tabele — cel de validare și cel de traducere — descriu aceleași
    chei din două locuri. Un reglaj adăugat într-unul singur e ori validat și
    nefolosit, ori folosit și nevalidat; ambele tac.
    """

    def test_validation_and_translation_cover_the_same_keys(self) -> None:
        self.assertEqual(
            set(FILE_PIPELINE_TUNABLES),
            set(agent.FILE_PIPELINE_CONFIG_TO_PARAMETER),
        )

    def test_every_translated_parameter_exists_on_the_monitor(self) -> None:
        """
        Un nume de parametru scris greșit în tabelul de traducere ar produce un
        TypeError abia la pornirea agentului, în producție. Aici pică la commit.
        """
        import inspect

        accepted = set(inspect.signature(FileMonitor.__init__).parameters)

        for parameter in agent.FILE_PIPELINE_CONFIG_TO_PARAMETER.values():
            with self.subTest(parameter=parameter):
                self.assertIn(parameter, accepted)


if __name__ == "__main__":
    unittest.main()
