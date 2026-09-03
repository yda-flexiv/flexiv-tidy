"""Command-line interface for flexiv-tidy."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from flexiv_tidy import assets_dir
from flexiv_tidy.project import ProjectError, discover_project, dispatch_script, tidy_config

STAGED_SUBDIR = "build/.flexiv-tidy"

USAGE = """\
usage: flexiv-tidy [--project PROJECT] {fix,clangd,install} [args...]

Flexiv clang-tidy fix and clangd-tidy speedup runner.

commands:
  fix <library> [options]   run clang-tidy and interactively review its fixes
  clangd [paths...] [options]
                            fast clangd-tidy diagnostics (default path: lib)
  install                   install the pinned clangd-tidy runtime in Docker

options:
  --project PROJECT         Flexiv worktree to operate on (default: cwd)
  -h, --help                show this help

All arguments after the command are forwarded verbatim to the underlying
bundled script. Run `flexiv-tidy fix -h` or `flexiv-tidy clangd -h` for the
forwarded script's own options.
"""


def _stage_assets(project: Path, names: list[str]) -> str:
    """Copy bundled scripts under the project so the Docker mount sees them.

    Returns the project-relative directory (``build/.flexiv-tidy``), which is
    the same path inside the container because the workspace is mounted at the
    same location.
    """
    assets = assets_dir()
    staged = project / STAGED_SUBDIR
    staged.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy2(assets / name, staged / name)
    return STAGED_SUBDIR


def _fix_command(project: Path, arguments: list[str]) -> int:
    assets = assets_dir()
    script = assets / "fix_clang_tidy.sh"
    reviewer = assets / "review_clang_tidy_fixes.py"
    config = tidy_config(project, assets / ".clang-tidy")

    env = os.environ.copy()
    env["FLEXIV_TIDY_PROJECT"] = str(project)
    env["FLEXIV_TIDY_REVIEWER"] = str(reviewer)
    env["FLEXIV_TIDY_CONFIG"] = str(config)
    env["FLEXIV_TIDY_PYTHON"] = sys.executable

    command = ["bash", str(script), *arguments]
    return subprocess.run(command, cwd=project, env=env).returncode


def _clangd_command(project: Path, arguments: list[str]) -> int:
    dispatch = dispatch_script(project)
    staged = _stage_assets(project, ["run_clangd_tidy_check.sh"])
    inner = shlex.join(["bash", f"{staged}/run_clangd_tidy_check.sh", *arguments])
    return subprocess.run([str(dispatch), "shell", inner], cwd=project).returncode


def _install_command(project: Path, arguments: list[str]) -> int:
    dispatch = dispatch_script(project)
    staged = _stage_assets(project, ["install_clangd_tidy_in_docker.sh"])
    inner = shlex.join(["bash", f"{staged}/install_clangd_tidy_in_docker.sh", *arguments])
    return subprocess.run([str(dispatch), "shell", inner], cwd=project).returncode


_COMMANDS = {
    "fix": _fix_command,
    "clangd": _clangd_command,
    "install": _install_command,
}


def _parse(argv: list[str]) -> tuple[str | None, str, list[str]]:
    """Return (project, command, passthrough) without rejecting script flags.

    Only ``--project`` and top-level ``--help`` are consumed here; every other
    token is either the command or forwarded verbatim to the bundled script, so
    flags such as ``fix --dry-run FvrRemoteParam`` pass through unchanged.
    """
    project: str | None = None
    command: str | None = None
    passthrough: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if command is not None:
            passthrough = argv[index:]
            break
        if token in ("-h", "--help"):
            print(USAGE, end="")
            raise SystemExit(0)
        if token == "--project":
            if index + 1 >= len(argv):
                print("flexiv-tidy: error: --project requires a value", file=sys.stderr)
                print(USAGE, file=sys.stderr)
                raise SystemExit(2)
            project = argv[index + 1]
            index += 2
            continue
        if token.startswith("--project="):
            project = token.split("=", 1)[1]
            index += 1
            continue
        if token in _COMMANDS:
            command = token
            index += 1
            continue
        print(f"flexiv-tidy: error: unknown command: {token}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)

    if command is None:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)

    return project, command, passthrough


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    project, command, passthrough = _parse(raw)
    try:
        project_path = discover_project(project)
        exit_code = _COMMANDS[command](project_path, passthrough)
    except ProjectError as error:
        print(f"flexiv-tidy: error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except OSError as error:
        print(f"flexiv-tidy: error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
