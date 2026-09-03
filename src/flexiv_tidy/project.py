"""Project discovery for flexiv-tidy."""

from __future__ import annotations

import subprocess
from pathlib import Path


class ProjectError(Exception):
    """The selected path is not a usable Flexiv worktree."""


def discover_project(path: str | Path | None = None) -> Path:
    """Return the top-level Git worktree containing *path*.

    Mirrors flexiv-steward's semantics: default to the current directory and
    resolve the enclosing Git worktree root.
    """
    candidate = Path(path or Path.cwd()).expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise ProjectError(
            f"{candidate} is not inside a Git worktree"
        ) from error
    return Path(result.stdout.strip()).resolve()


def dispatch_script(project: Path) -> Path:
    """Return the project's docker dispatcher, failing loudly if missing."""
    dispatch = project / "docker" / "docker_dispatch.sh"
    if not dispatch.is_file():
        raise ProjectError(
            f"{dispatch} not found; is {project} a Flexiv worktree?"
        )
    return dispatch


def tidy_config(project: Path, fallback: Path) -> Path:
    """Prefer the project's canonical config, falling back to the bundled one."""
    config = project / "cmake" / "tools" / ".clang-tidy"
    return config if config.is_file() else fallback
