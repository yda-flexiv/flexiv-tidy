"""Flexiv clang-tidy fix and clangd-tidy speedup runner."""

from __future__ import annotations

from pathlib import Path


def assets_dir() -> Path:
    """Return the directory holding the bundled shell scripts and config files."""
    return Path(__file__).resolve().parent / "assets"


def main() -> None:
    """Run the command-line interface without importing it during package load."""
    from flexiv_tidy.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
