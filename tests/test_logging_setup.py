"""
Teste pentru izolarea configurării de jurnalizare.

De ce există acest fișier:
    agent.py configura jurnalizarea la nivel de modul, deci simplul
    `import agent` — inclusiv cel făcut de colectarea testelor — deschidea
    agent.log din rădăcina repo-ului și instala handlerele pe root logger.
    Fiind pe root, ele prind înregistrările oricărui logger din proces, nu doar
    ale lui agent.logger: patch-ul obișnuit din teste nu apăra fișierul. O
    rulare suficient de zgomotoasă putea declanșa rotația la 10 MB peste date
    forensice reale, iar pe Windows handle-ul rămânea deschis peste jurnalul
    unui agent aflat în execuție.

    Regresia e tăcută: dacă setup-ul se întoarce la nivel de modul, toate
    testele trec în continuare. Singurul semnal este cel verificat aici — după
    import, niciun handler de pe root logger nu trebuie să scrie în agent.log.
"""

import logging
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import TemporaryDirectory

import agent


# Calea este recalculată din locația modulului, nu citită din constanta privată
# _LOG_FILE_PATH: testul trebuie să pice dacă jurnalul de producție e redeschis
# la import, indiferent cum ajunge să se numească constanta care îl descrie.
_PRODUCTION_LOG_PATH = Path(agent.__file__).resolve().parent / "agent.log"


def _root_handlers_writing_to(path: Path) -> list:
    """Handlerele de fișier de pe root logger care scriu în `path`."""
    target = path.resolve()
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename).resolve() == target
    ]


class LoggingSetupIsolationTests(unittest.TestCase):
    """Jurnalizarea se instalează la pornirea procesului, nu la import."""

    def test_importing_agent_does_not_open_the_production_log(self) -> None:
        """Importul modulului rămâne inert față de jurnalul operatorului."""
        self.assertEqual(
            _root_handlers_writing_to(_PRODUCTION_LOG_PATH),
            [],
            "Importul lui agent a atașat un handler pe agent.log: rularea "
            "testelor scrie în jurnalul de producție și îl poate roti.",
        )

    def test_configure_logging_installs_a_rotating_file_handler(self) -> None:
        """Instalarea la cerere funcționează și păstrează rotația activă."""
        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level

        with TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "agent.log"
            try:
                # Root-ul e golit înainte de apel: configure_logging folosește
                # force=True, care ar închide handlerele instalate de runner.
                # Ele sunt puse la loc în finally, neatinse.
                root.handlers[:] = []
                agent.configure_logging(log_file_path=log_path)

                installed = _root_handlers_writing_to(log_path)
                self.assertEqual(len(installed), 1)
                self.assertIsInstance(installed[0], RotatingFileHandler)

                # Valorile exacte nu sunt fixate aici: contează că rotația e
                # activă, nu pragul ales, care poate fi reglat fără regresie.
                self.assertGreater(installed[0].maxBytes, 0)
                self.assertGreater(installed[0].backupCount, 0)
            finally:
                # Handlerele noi se închid înainte de ștergerea directorului
                # temporar: pe Windows un fișier cu handle deschis nu poate fi
                # șters, iar cleanup-ul ar arunca PermissionError.
                for handler in root.handlers:
                    if handler not in saved_handlers:
                        handler.close()
                root.handlers[:] = saved_handlers
                root.setLevel(saved_level)


if __name__ == "__main__":
    unittest.main()