import errno
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest

from src.bridge.secrets import load_credentials


class CliTests(unittest.TestCase):
    def test_interactive_provisioning_hides_input_and_does_not_use_argv_or_env(self):
        username, password = b"SyntheticPtyOnly", b"synthetic-pty-private-42!"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "iccup.secret"
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, "-m", "src.bridge", "secret-create", "--path", str(path)],
                                       stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            transcript = bytearray()
            try:
                for prompt, value in ((b"username (hidden): ", username), (b"Password (hidden): ", password),
                                      (b"Repeat password (hidden): ", password)):
                    deadline = time.monotonic() + 5
                    while prompt not in transcript:
                        self.assertLess(time.monotonic(), deadline, "interactive secret prompt missing")
                        if select.select([master], [], [], .1)[0]:
                            transcript.extend(os.read(master, 4096))
                    # Check the actual process surfaces while it is alive.
                    for proc_file in ("cmdline", "environ"):
                        surface = Path(f"/proc/{process.pid}/{proc_file}").read_bytes()
                        self.assertNotIn(password, surface)
                        self.assertNotIn(username, surface)
                    os.write(master, value + b"\n")
                process.wait(timeout=5)
                while select.select([master], [], [], .1)[0]:
                    try:
                        data = os.read(master, 4096)
                        if not data:
                            break
                        transcript.extend(data)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                self.assertEqual(process.returncode, 0)
                self.assertNotIn(password, transcript)
                self.assertNotIn(username, transcript)
                self.assertIn(b"SECRET_CREATED", transcript)
                self.assertEqual(load_credentials(path).password, password.decode())
            finally:
                os.close(master)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)

    def test_status_remains_blocked_with_nonzero_exit(self):
        result = subprocess.run([sys.executable, "-m", "src.bridge", "status"], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 78)
        self.assertFalse(json.loads(result.stdout)["iccup_authenticated"])
        self.assertFalse(json.loads(result.stdout)["ready"])


if __name__ == "__main__":
    unittest.main()
