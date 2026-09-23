import io
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from flexiv_tidy.cli import _clangd_command, _fix_command, _parse, main


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

    def test_fix_stages_fallback_config_for_docker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tidy project ") as temporary:
            project = Path(temporary)
            with patch("flexiv_tidy.cli.subprocess.run") as run:
                run.return_value.returncode = 0
                self.assertEqual(_fix_command(project, ["Example"]), 0)
            config = Path(run.call_args.kwargs["env"]["FLEXIV_TIDY_CONFIG"])
            self.assertTrue(config.is_relative_to(project))
            self.assertTrue(config.is_file())

    def test_clangd_uses_project_or_staged_config(self) -> None:
        for canonical in (False, True):
            with self.subTest(canonical=canonical), tempfile.TemporaryDirectory(
                prefix="tidy project "
            ) as temporary:
                project = Path(temporary)
                dispatch = project / "docker/docker_dispatch.sh"
                dispatch.parent.mkdir()
                dispatch.touch()
                config = project / "cmake/tools/.clang-tidy"
                if canonical:
                    config.parent.mkdir(parents=True)
                    config.write_text("Checks: '*'\n")
                else:
                    config = project / "build/.flexiv-tidy/.clang-tidy"
                with patch("flexiv_tidy.cli.subprocess.run") as run:
                    run.return_value.returncode = 0
                    _clangd_command(project, ["lib/path with spaces"])
                inner = shlex.split(run.call_args.args[0][2])
                self.assertEqual(inner[:2], ["env", f"FLEXIV_TIDY_CONFIG={config}"])
                self.assertEqual(inner[-1], "lib/path with spaces")
                self.assertTrue(config.is_file())


if __name__ == "__main__":
    unittest.main()
