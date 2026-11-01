import contextlib
import io
import json
import getpass
import os
from pathlib import Path
import stat
import tempfile
import unittest
import warnings
from unittest.mock import patch

from src.bridge import start, status
from src.bridge.__main__ import main
from src.bridge.secrets import Credentials, SecretError, load_credentials, provision_credentials
from src.connector import UnsupportedAuthentication


class SecretTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "account.secret"
        self.username, self.password = "SyntheticOnly", "synthetic-DO-NOT-LOG-9!"

    def create(self):
        provision_credentials(self.path, self.username, self.password)

    def raw(self, data):
        self.path.write_bytes(data)
        self.path.chmod(0o600)

    def test_round_trip_and_private_mode_under_umask(self):
        previous = os.umask(0o777)
        try:
            self.create()
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.path.stat().st_uid, os.geteuid())
        for mode in (0o600, 0o400):
            self.path.chmod(mode)
            credentials = load_credentials(self.path)
            self.assertEqual(credentials.username, self.username)
            self.assertEqual(credentials.password, self.password)
            self.assertNotIn(self.password, repr(credentials))
            self.assertNotIn(self.username, str(credentials))

    def test_existing_file_is_never_overwritten(self):
        self.create()
        original = self.path.read_bytes()
        with self.assertRaises(SecretError):
            provision_credentials(self.path, "other", "different")
        self.assertEqual(self.path.read_bytes(), original)

    def test_symlink_and_symlink_parent_are_rejected(self):
        self.create()
        alias = self.root / "alias.secret"
        alias.symlink_to(self.path)
        with self.assertRaises(SecretError):
            load_credentials(alias)
        with self.assertRaises(SecretError):
            provision_credentials(alias, "other", "different")
        directory_alias = self.root / "directory-alias"
        directory_alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(SecretError):
            load_credentials(directory_alias / self.path.name)
        with self.assertRaises(SecretError):
            provision_credentials(directory_alias / "new.secret", "other", "different")
        self.assertFalse((self.root / "new.secret").exists())

    def test_hard_link_and_special_files_are_rejected_without_blocking(self):
        self.create()
        alias = self.root / "hard.secret"
        os.link(self.path, alias)
        with self.assertRaises(SecretError):
            load_credentials(alias)
        fifo = self.root / "fifo.secret"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(SecretError):
            load_credentials(fifo)
        with self.assertRaises(SecretError):
            load_credentials(self.root)

    def test_group_other_execute_and_special_modes_are_rejected(self):
        self.create()
        for mode in (0o644, 0o640, 0o660, 0o604, 0o700, 0o4600):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                with self.assertRaises(SecretError):
                    load_credentials(self.path)

    def test_wrong_owner_is_rejected(self):
        self.create()
        with patch("src.bridge.secrets.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(SecretError, "OWNER_MISMATCH"):
                load_credentials(self.path)

    def test_provision_requires_private_owned_parent(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(SecretError, "PARENT"):
            self.create()
        self.assertFalse(self.path.exists())

    def test_unbounded_malformed_duplicate_and_control_inputs_are_rejected(self):
        base = {"version": 1, "username": self.username, "password": self.password}
        bad = [b"", b"x" * 4097, b"\xff", b"[", b"[]", b"null",
               b'{"version":1,"version":1,"username":"x","password":"y"}']
        for changes in ({"version": True}, {"extra": "data"}, {"password": ""},
                        {"password": "a\nb"}, {"username": "a\x00b"}, {"password": "\ud800"},
                        {"password": 123}, {"username": "x" * 129}):
            bad.append(json.dumps(base | changes).encode())
        for data in bad:
            self.raw(data)
            with self.assertRaises(SecretError) as raised:
                load_credentials(self.path)
            self.assertNotIn(self.password, str(raised.exception))

    def test_relative_dot_and_missing_paths_fail_redacted(self):
        for path in ("relative.secret", f"{self.root}/../account.secret", f"{self.root}//file.secret", self.path):
            with self.assertRaises(SecretError) as raised:
                load_credentials(path)
            self.assertNotIn(str(path), str(raised.exception))

    def test_cli_check_prints_no_credentials_or_raw_parse_error(self):
        self.create()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["secret-check", "--path", str(self.path)]), 0)
        self.raw(("{BAD:" + self.password).encode())
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["secret-check", "--path", str(self.path)]), 2)
        self.assertNotIn(self.password, output.getvalue())
        self.assertNotIn(self.username, output.getvalue())
        self.assertNotIn(str(self.path), output.getvalue())

    def test_cli_create_refuses_piped_password_input(self):
        output = io.StringIO()
        with patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(output):
            self.assertEqual(main(["secret-create", "--path", str(self.path)]), 2)
        self.assertFalse(self.path.exists())
        self.assertIn("INTERACTIVE_TTY_REQUIRED", output.getvalue())

    def test_cli_create_refuses_getpass_echo_fallback(self):
        def unsafe_fallback(_prompt):
            warnings.warn("do not echo", getpass.GetPassWarning)
            self.fail("getpass echo fallback must not consume input")

        output = io.StringIO()
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True), \
             patch("src.bridge.__main__.getpass.getpass", side_effect=unsafe_fallback), \
             contextlib.redirect_stdout(output):
            self.assertEqual(main(["secret-create", "--path", str(self.path)]), 2)
        self.assertFalse(self.path.exists())
        self.assertIn("SECRET_SECURE_TTY_REQUIRED", output.getvalue())

    def test_bridge_gate_performs_no_io_and_cannot_be_lifted_by_probe(self):
        with patch("socket.socket", side_effect=AssertionError("no socket")), \
             patch("subprocess.Popen", side_effect=AssertionError("no child")), \
             patch("src.bridge.secrets.load_credentials", side_effect=AssertionError("no secret")):
            with self.assertRaises(UnsupportedAuthentication):
                start()
        snapshot = status()
        self.assertEqual(snapshot["state"], "BLOCKED")
        self.assertFalse(snapshot["ready"])
        snapshot["ready"] = True
        self.assertFalse(status()["ready"])


if __name__ == "__main__":
    unittest.main()
