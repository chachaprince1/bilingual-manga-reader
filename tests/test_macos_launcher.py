from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "posix", "the macOS bundle launcher is a POSIX shell script")
class MacOSBundleLauncherTests(unittest.TestCase):
    def test_bundle_launcher_detaches_and_scrubs_launchservices_identity(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        launcher_source = project_root / "packaging" / "macos-launcher.sh"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "Bilingual Manga Reader and Mokuro Converter.app" / "Contents"
            main = app / "MacOS" / "Bilingual Manga Reader and Mokuro Converter"
            runtime = app / "Resources" / "runtime" / "Bilingual Manga Reader and Mokuro Converter"
            result_file = root / "runtime-result.txt"
            data_root = root / "data"

            main.parent.mkdir(parents=True)
            runtime.parent.mkdir(parents=True)
            main.write_bytes(launcher_source.read_bytes())
            main.chmod(0o755)
            runtime.write_text(
                "#!/bin/sh\n"
                "sleep 0.75\n"
                "printf '%s\\n%s\\n%s\\n' "
                '"${__CFBundleIdentifier-unset}" '
                '"${XPC_SERVICE_NAME-unset}" '
                '"${BMO_DATA_DIR-unset}" > "$BMO_TEST_RESULT"\n',
                encoding="utf-8",
            )
            runtime.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "BMO_DATA_DIR": str(data_root),
                    "BMO_TEST_RESULT": str(result_file),
                    "__CFBundleIdentifier": "org.bilingualmanga.offline",
                    "XPC_SERVICE_NAME": "application.org.bilingualmanga.offline.test",
                }
            )
            subprocess.run([str(main)], env=environment, check=True, timeout=2)
            self.assertFalse(result_file.exists(), "bundle launcher waited for the detached runtime")

            expected = ["unset", "unset", str(data_root)]
            deadline = time.monotonic() + 2
            actual = []
            while time.monotonic() < deadline:
                if result_file.exists():
                    actual = result_file.read_text(encoding="utf-8").splitlines()
                    if actual == expected:
                        break
                time.sleep(0.02)
            self.assertEqual(
                actual,
                expected,
            )


if __name__ == "__main__":
    unittest.main()
