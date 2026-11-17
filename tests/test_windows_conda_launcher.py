"""Verify that a Conda batch shim returns control to the deployment script."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Requires Windows cmd.exe")
class CondaLauncherTests(unittest.TestCase):
    def test_conda_batch_failure_reaches_error_handler(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "deploy_windows_conda.bat"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls.txt"
            (root / "conda.bat").write_text(
                '@echo off\n'
                'echo %*>>"%CONDA_TEST_CALLS%"\n'
                'exit /b 17\n',
                encoding="ascii",
            )
            env = dict(os.environ)
            env["PATH"] = str(root) + os.pathsep + env["PATH"]
            env["CONDA_TEST_CALLS"] = str(calls)
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", str(script)],
                env=env, input="\n", capture_output=True, text=True,
                errors="replace", timeout=15,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("Deployment or startup failed.", result.stdout)
            self.assertEqual(len(calls.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
