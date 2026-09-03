import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from flexiv_tidy.cli import _parse, main


class CliTests(unittest.TestCase):
    def _invoke(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exit_error:
                main(list(argv))
        return exit_error.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_help_lists_commands(self) -> None:
        code, stdout, stderr = self._invoke("--help")
        self.assertEqual(code, 0)
        self.assertIn("fix", stdout)
        self.assertIn("clangd", stdout)
        self.assertIn("install", stdout)
        self.assertEqual(stderr, "")

    def test_missing_project_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, stderr = self._invoke(
                "--project", tmp, "fix", "FvrRemoteParam", "--dry-run"
            )
        self.assertEqual(code, 1)
        self.assertIn("is not inside a Git worktree", stderr)

    def test_parse_forwards_flags_first(self) -> None:
        project, command, passthrough = _parse(["fix", "--dry-run", "FvrRemoteParam"])
        self.assertIsNone(project)
        self.assertEqual(command, "fix")
        self.assertEqual(passthrough, ["--dry-run", "FvrRemoteParam"])

    def test_parse_project_and_command(self) -> None:
        project, command, passthrough = _parse(
            ["--project", "/tmp/wt", "clangd", "lib", "-j", "8"]
        )
        self.assertEqual(project, "/tmp/wt")
        self.assertEqual(command, "clangd")
        self.assertEqual(passthrough, ["lib", "-j", "8"])


if __name__ == "__main__":
    unittest.main()
