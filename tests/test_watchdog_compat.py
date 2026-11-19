"""
Corectura de oprire peste watchdog 7.1.0 (services/watchdog_compat.py).

Cursa reală e rară, deci nu se poate aștepta într-un test. Se reproduce
determinist: prima anulare se pierde, exact ce se întâmplă când CancelIoEx
sosește înainte ca firul cititor să intre în ReadDirectoryChangesExW. Cu
oprirea originală, firul rămâne blocat până la următoarea schimbare din
director, iar oprirea nu se mai întoarce.
"""

import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from watchdog.version import VERSION_STRING

from services import watchdog_compat


class WatchdogVersionTests(unittest.TestCase):
    def test_the_installed_watchdog_is_the_one_the_patch_was_checked_against(self) -> None:
        """
        Dacă pică: watchdog s-a schimbat. Verifică dacă winapi.DirectoryChangeReader
        mai are cursa la oprire, actualizează sau scoate corectura, apoi
        PATCHED_WATCHDOG_VERSION.
        """
        self.assertEqual(VERSION_STRING, watchdog_compat.PATCHED_WATCHDOG_VERSION)


@unittest.skipUnless(os.name == "nt", "cititorul ReadDirectoryChangesW e specific Windows")
class ReaderStopTests(unittest.TestCase):
    def setUp(self) -> None:
        from watchdog.observers import winapi

        self.winapi = winapi
        self.assertTrue(watchdog_compat.apply())
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def _stop_in_background(self, reader) -> threading.Thread:
        stopper = threading.Thread(target=reader.stop, daemon=True)
        stopper.start()
        stopper.join(timeout=5)
        return stopper

    def test_the_patch_is_in_place(self) -> None:
        self.assertIs(
            self.winapi.DirectoryChangeReader.stop,
            watchdog_compat._stop_until_the_reader_exits,
        )

    def test_a_lost_cancellation_does_not_hang_the_stop(self) -> None:
        reader = self.winapi.DirectoryChangeReader(self.directory, recursive=True)
        reader.start()

        original_cancel = self.winapi._cancel_handle_io
        calls = []

        def lose_the_first_cancellation(handle) -> None:
            calls.append(handle)
            if len(calls) > 1:
                original_cancel(handle)

        with patch.object(self.winapi, "_cancel_handle_io", lose_the_first_cancellation):
            stopper = self._stop_in_background(reader)

        self.assertFalse(stopper.is_alive(), "oprirea a rămas blocată după o anulare pierdută")
        self.assertGreaterEqual(len(calls), 2)

    def test_a_normal_stop_returns_and_releases_the_handle(self) -> None:
        reader = self.winapi.DirectoryChangeReader(self.directory, recursive=True)
        reader.start()

        stopper = self._stop_in_background(reader)

        self.assertFalse(stopper.is_alive())
        self.assertIsNone(reader._handle)

    def test_a_second_stop_returns_at_once(self) -> None:
        reader = self.winapi.DirectoryChangeReader(self.directory, recursive=True)
        reader.start()
        reader.stop()

        with patch.object(self.winapi, "_cancel_handle_io") as cancel:
            reader.stop()

        cancel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
