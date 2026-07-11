import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import config_loader


class ConfigLoaderPathTests(unittest.TestCase):
    def _write_config(self, path: Path, agent_id: str) -> None:
        path.write_text(
            json.dumps(
                {
                    "agent_id": agent_id,
                    "server_url": "http://127.0.0.1:8000",
                    "agent_version": "1.0.0",
                    "monitored_directories": [str(path.parent.resolve())],
                }
            ),
            encoding="utf-8",
        )

    def test_default_config_path_is_independent_of_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as config_dir, tempfile.TemporaryDirectory() as cwd:
            config_path = Path(config_dir) / "config.json"
            self._write_config(config_path, "installation-config")

            with patch.object(config_loader, "CONFIG_PATH", config_path):
                previous_cwd = Path.cwd()
                try:
                    os.chdir(cwd)
                    loaded = config_loader.load_config()
                finally:
                    os.chdir(previous_cwd)

            self.assertEqual(loaded["agent_id"], "installation-config")

    def test_explicit_config_path_still_overrides_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.json"
            self._write_config(config_path, "explicit-config")

            loaded = config_loader.load_config(config_path)

            self.assertEqual(loaded["agent_id"], "explicit-config")


if __name__ == "__main__":
    unittest.main()