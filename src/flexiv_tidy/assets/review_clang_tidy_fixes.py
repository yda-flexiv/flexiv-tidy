#!/usr/bin/env python3
"""Interactively review clang-tidy diagnostics and suggested replacements."""

from __future__ import annotations

import argparse
import difflib
import json
import mimetypes
import os
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import webbrowser
from collections import defaultdict
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inc"}
WEB_ASSETS = {
    "/": "review_clang_tidy_web.html",
    "/app.css": "review_clang_tidy_web.css",
    "/app.js": "review_clang_tidy_web.js",
}


@dataclass(frozen=True)
class Replacement:
    path: Path
    offset: int
    length: int
    text: bytes

    @property
    def end(self) -> int:
        return self.offset + self.length


@dataclass(frozen=True)
class Finding:
    name: str
    message: str
    path: Path | None
    offset: int
    replacements: tuple[Replacement, ...]

    @property
    def paths(self) -> frozenset[Path]:
        """All files whose snapshot may be relevant to this diagnostic."""
        paths = {replacement.path for replacement in self.replacements}
        if self.path is not None:
            paths.add(self.path)
        return frozenset(paths)


class ReviewError(Exception):
    """A proposed replacement cannot safely be applied to the current buffers."""


def scoped_path(raw_path: str, build_directory: str, library_root: Path) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(build_directory) / path
    path = path.resolve()
    try:
        path.relative_to(library_root)
    except ValueError:
        return None
    if any("generated" in part.lower() for part in path.parts):
        return None
    if path.suffix.lower() not in SOURCE_SUFFIXES or not path.is_file():
        return None
    return path


def load_findings(fixes_file: Path, library_root: Path) -> tuple[list[Finding], int]:
    try:
        document: dict[str, Any] = yaml.safe_load(fixes_file.read_text()) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ReviewError(f"cannot read clang-tidy fixes: {error}") from error

    findings: list[Finding] = []
    ignored = 0
    seen: set[tuple[Any, ...]] = set()
    for raw_finding in document.get("Diagnostics", []):
        diagnostic = raw_finding.get("DiagnosticMessage", {}) or {}
        build_directory = str(raw_finding.get("BuildDirectory", ""))
        diagnostic_path = scoped_path(
            str(diagnostic.get("FilePath", "")), build_directory, library_root
        )
        replacements: list[Replacement] = []
        unsafe_replacement = False
        for raw_replacement in diagnostic.get(
            "Replacements", raw_finding.get("Replacements", [])
        ):
            path = scoped_path(
                str(raw_replacement.get("FilePath", "")), build_directory, library_root
            )
            if path is None:
                unsafe_replacement = True
                continue
            try:
                replacement = Replacement(
                    path=path,
                    offset=int(raw_replacement["Offset"]),
                    length=int(raw_replacement["Length"]),
                    text=str(raw_replacement.get("ReplacementText", "")).encode("utf-8"),
                )
            except (KeyError, TypeError, ValueError):
                unsafe_replacement = True
                continue
            replacements.append(replacement)

        # Never present a partially scoped multi-edit fix as safe. A diagnostic
        # without a fix remains reviewable if its primary location is in scope.
        if unsafe_replacement and replacements:
            ignored += 1
            replacements = []
        if diagnostic_path is None and not replacements:
            ignored += 1
            continue

        name = str(raw_finding.get("DiagnosticName", "clang-tidy"))
        message = str(diagnostic.get("Message", ""))
        offset = int(diagnostic.get("FileOffset", 0) or 0)
        fingerprint = (
            name,
            message,
            str(diagnostic_path),
            offset,
            tuple((str(item.path), item.offset, item.length, item.text) for item in replacements),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        findings.append(
            Finding(name, message, diagnostic_path, offset, tuple(replacements))
        )

    findings.sort(key=lambda item: (str(item.path), item.offset, item.name, item.message))
    return findings, ignored


def ranges_conflict(left: Replacement, right: Replacement) -> bool:
    if left.path != right.path:
        return False
    if left.length == 0 and right.length == 0:
        return left.offset == right.offset
    if left.length == 0:
        return right.offset <= left.offset < right.end
    if right.length == 0:
        return left.offset <= right.offset < left.end
    return left.offset < right.end and right.offset < left.end


class ReviewState:
    def __init__(
        self,
        findings: list[Finding],
        library_root: Path,
        display_root: Path | None = None,
    ) -> None:
        self.library_root = library_root
        self.display_root = display_root or library_root.parents[2]
        paths = {path for finding in findings for path in finding.paths}
        self.original = {path: path.read_bytes() for path in paths}
        self.current = dict(self.original)
        self.accepted: dict[Path, list[Replacement]] = defaultdict(list)
        self.manually_edited: set[Path] = set()

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.display_root))
        except ValueError:
            return str(path)

    def invalidated_paths(self, finding: Finding) -> frozenset[Path]:
        """Return files that make a finding stale after a full-file manual edit.

        clang-tidy offsets and messages describe the snapshot exported before the
        review started. Once a user edits a complete file, there is no generally
        safe way to decide whether a later diagnostic remains valid or to rebase
        all of its byte offsets. The whole finding is therefore deferred until a
        fresh clang-tidy run if any file it touches was manually edited.
        """
        return finding.paths & self.manually_edited

    def _adjusted_offset(self, replacement: Replacement) -> int:
        shift = 0
        for accepted in self.accepted[replacement.path]:
            if accepted.end <= replacement.offset:
                shift += len(accepted.text) - accepted.length
        return replacement.offset + shift

    def candidate(self, finding: Finding) -> dict[Path, bytes]:
        invalidated = self.invalidated_paths(finding)
        if invalidated:
            labels = ", ".join(sorted(self.relative(path) for path in invalidated))
            raise ReviewError(
                f"{labels} was manually edited; rerun clang-tidy before reviewing this diagnostic"
            )

        grouped: dict[Path, list[Replacement]] = defaultdict(list)
        for replacement in finding.replacements:
            grouped[replacement.path].append(replacement)

        result: dict[Path, bytes] = {}
        for path, replacements in grouped.items():
            for index, replacement in enumerate(replacements):
                for other in replacements[index + 1 :]:
                    if ranges_conflict(replacement, other):
                        raise ReviewError("this suggestion contains overlapping replacements")
                for accepted in self.accepted[path]:
                    if ranges_conflict(replacement, accepted):
                        raise ReviewError("this suggestion overlaps an already accepted replacement")

            data = self.current[path]
            edits: list[tuple[int, int, bytes]] = []
            for replacement in replacements:
                adjusted = self._adjusted_offset(replacement)
                expected = self.original[path][replacement.offset : replacement.end]
                if data[adjusted : adjusted + replacement.length] != expected:
                    raise ReviewError("the file no longer matches clang-tidy's analyzed contents")
                edits.append((adjusted, replacement.length, replacement.text))
            for offset, length, text in sorted(edits, reverse=True):
                data = data[:offset] + text + data[offset + length :]
            result[path] = data
        return result

    def accept(self, finding: Finding, candidate: dict[Path, bytes], manual: bool) -> None:
        for path, data in candidate.items():
            self.current[path] = data
        if manual:
            # Even if the editor happened to produce the same bytes as clang-tidy's
            # proposal, it was allowed to change the full file. Original offsets for
            # all later findings in these files must be treated as stale.
            self.manually_edited.update(candidate)
        else:
            for replacement in finding.replacements:
                self.accepted[replacement.path].append(replacement)

    def changed(self) -> dict[Path, bytes]:
        return {
            path: data for path, data in self.current.items() if data != self.original[path]
        }

    def write(self) -> list[Path]:
        changed = self.changed()
        for path in changed:
            if path.read_bytes() != self.original[path]:
                raise ReviewError(
                    f"{self.relative(path)} changed during review; refusing to overwrite it"
                )

        written = []
        for path, data in changed.items():
            mode = stat.S_IMODE(path.stat().st_mode)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.clang-tidy-", dir=path.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                os.chmod(temporary, mode)
                os.replace(temporary, path)
                written.append(path)
            finally:
                temporary.unlink(missing_ok=True)
        return written


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def unified_diff(
    before: dict[Path, bytes], after: dict[Path, bytes], root: Path, color: bool
) -> str:
    output: list[str] = []
    for path in sorted(set(before) | set(after)):
        old_data = before.get(path, b"")
        new_data = after.get(path, old_data)
        if old_data == new_data:
            continue
        old_lines = old_data.decode("utf-8", errors="replace").splitlines(keepends=True)
        new_lines = new_data.decode("utf-8", errors="replace").splitlines(keepends=True)
        label = display_path(path, root)
        for line in difflib.unified_diff(
            old_lines, new_lines, fromfile=f"before/{label}", tofile=f"after/{label}"
        ):
            if color:
                if line.startswith("+++") or line.startswith("---"):
                    line = f"\033[1m{line}\033[0m"
                elif line.startswith("+"):
                    line = f"\033[32m{line}\033[0m"
                elif line.startswith("-"):
                    line = f"\033[31m{line}\033[0m"
                elif line.startswith("@@"):
                    line = f"\033[36m{line}\033[0m"
            output.append(line)
    return "".join(output)


def numbered_file(path: Path, data: bytes, title: str) -> str:
    lines = data.decode("utf-8", errors="replace").splitlines()
    width = len(str(max(len(lines), 1)))
    body = "\n".join(f"{index:>{width}} | {line}" for index, line in enumerate(lines, 1))
    return f"\n--- {title}: {path} ---\n{body}\n"


def diagnostic_context(finding: Finding, state: ReviewState, radius: int = 3) -> str:
    if finding.path is None:
        return ""
    data = state.original[finding.path]
    line_number = data[: finding.offset].count(b"\n") + 1
    lines = state.current[finding.path].decode("utf-8", errors="replace").splitlines()
    first = max(1, line_number - radius)
    last = min(len(lines), line_number + radius)
    width = len(str(max(last, 1)))
    output = []
    for number in range(first, last + 1):
        marker = ">" if number == line_number else " "
        output.append(f"{marker} {number:>{width}} | {lines[number - 1]}")
    return "\n".join(output)


def edit_candidate(candidate: dict[Path, bytes]) -> dict[Path, bytes]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vim"
    command = shlex.split(editor)
    if not command:
        raise ReviewError("VISUAL/EDITOR is empty")

    temporary_paths: dict[Path, Path] = {}
    try:
        for path, data in candidate.items():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f"{path.stem}.clang-tidy-", suffix=path.suffix
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            temporary_paths[path] = temporary
            print(f"Editing proposed {path} as {temporary}")
        result = subprocess.run([*command, *(str(path) for path in temporary_paths.values())])
        if result.returncode != 0:
            raise ReviewError(f"editor exited with status {result.returncode}")
        return {path: temporary.read_bytes() for path, temporary in temporary_paths.items()}
    except OSError as error:
        raise ReviewError(f"cannot launch editor: {error}") from error
    finally:
        for temporary in temporary_paths.values():
            temporary.unlink(missing_ok=True)


def prompt(text: str) -> str:
    try:
        return input(text).strip().lower()
    except EOFError:
        return "x"


def source_location(data: bytes, offset: int) -> tuple[int, int]:
    """Return a one-based line and column for a byte offset."""
    safe_offset = max(0, min(offset, len(data)))
    before = data[:safe_offset]
    return before.count(b"\n") + 1, safe_offset - before.rfind(b"\n")


class WebReviewSession:
    """State and JSON-facing operations for one browser review session."""

    def __init__(self, findings: list[Finding], state: ReviewState, ignored: int) -> None:
        self.findings = findings
        self.state = state
        self.ignored = ignored
        self.statuses = ["pending"] * len(findings)
        self.finished = threading.Event()
        self.result = 0
        self.outcome = "reviewing"

    def _finding(self, finding_id: int) -> tuple[int, Finding]:
        index = finding_id - 1
        if index < 0 or index >= len(self.findings):
            raise ReviewError(f"unknown finding: {finding_id}")
        return index, self.findings[index]

    def _sync_stale(self) -> None:
        for index, finding in enumerate(self.findings):
            if self.statuses[index] == "pending" and self.state.invalidated_paths(finding):
                self.statuses[index] = "stale"

    def _summary(self) -> dict[str, int]:
        counts = {name: self.statuses.count(name) for name in {
            "pending", "accepted", "rejected", "deferred", "stale"
        }}
        counts["changed_files"] = len(self.state.changed())
        counts["total"] = len(self.findings)
        counts["reviewed"] = counts["total"] - counts["pending"]
        counts["ignored"] = self.ignored
        return counts

    def snapshot(self) -> dict[str, Any]:
        self._sync_stale()
        items = []
        for index, finding in enumerate(self.findings):
            location_data = (
                self.state.original.get(finding.path, b"") if finding.path else b""
            )
            line, column = source_location(location_data, finding.offset)
            items.append({
                "id": index + 1,
                "check": finding.name,
                "message": finding.message,
                "path": self.state.relative(finding.path) if finding.path else "unknown",
                "line": line,
                "column": column,
                "fixable": bool(finding.replacements),
                "status": self.statuses[index],
                "files": sorted(self.state.relative(path) for path in finding.paths),
            })
        return {
            "summary": self._summary(),
            "findings": items,
            "outcome": self.outcome,
        }

    def detail(self, finding_id: int) -> dict[str, Any]:
        index, finding = self._finding(finding_id)
        invalidated = self.state.invalidated_paths(finding)
        candidate: dict[Path, bytes] = {}
        error = ""
        if invalidated:
            labels = ", ".join(sorted(self.state.relative(path) for path in invalidated))
            error = f"{labels} was manually edited; rerun clang-tidy to refresh this finding."
        elif finding.replacements:
            try:
                candidate = self.state.candidate(finding)
            except ReviewError as review_error:
                error = str(review_error)

        paths = sorted(candidate or finding.paths)
        files = []
        for path in paths:
            before = self.state.current[path]
            proposed = candidate.get(path, before)
            files.append({
                "path": self.state.relative(path),
                "before": before.decode("utf-8", errors="replace"),
                "proposed": proposed.decode("utf-8", errors="replace"),
                "changed": before != proposed,
            })
        return {
            "finding": self.snapshot()["findings"][index],
            "files": files,
            "diff": unified_diff(self.state.current, candidate, self.state.display_root, False),
            "error": error,
        }

    def decide(
        self,
        finding_id: int,
        decision: str,
        edits: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        index, finding = self._finding(finding_id)
        if self.statuses[index] != "pending":
            raise ReviewError("this finding has already been reviewed")
        if self.state.invalidated_paths(finding):
            self.statuses[index] = "stale"
            raise ReviewError("this finding became stale after a manual edit")

        if decision == "reject":
            self.statuses[index] = "rejected"
        elif decision == "defer":
            self.statuses[index] = "deferred"
        elif decision == "accept":
            if not finding.replacements and not edits:
                raise ReviewError("this finding has no automatic fix; edit a file first")
            candidate = self.state.candidate(finding) if finding.replacements else {}
            manual = edits is not None
            if edits is not None:
                if not all(
                    isinstance(label, str) and isinstance(contents, str)
                    for label, contents in edits.items()
                ):
                    raise ReviewError("edited file names and contents must be strings")
                editable = candidate or (
                    {finding.path: self.state.current[finding.path]}
                    if finding.path is not None else {}
                )
                by_label = {self.state.relative(path): path for path in editable}
                if set(edits) != set(by_label):
                    raise ReviewError("edited files do not match this finding")
                candidate = {
                    by_label[label]: contents.encode("utf-8")
                    for label, contents in edits.items()
                }
            if not candidate:
                raise ReviewError("no in-scope file is available to change")
            self.state.accept(finding, candidate, manual=manual)
            self.statuses[index] = "accepted"
            self._sync_stale()
        else:
            raise ReviewError(f"unknown decision: {decision}")
        return self.snapshot()

    def final_diff(self) -> dict[str, Any]:
        changed = self.state.changed()
        return {
            "diff": unified_diff(self.state.original, self.state.current, self.state.display_root, False),
            "files": sorted(self.state.relative(path) for path in changed),
            "summary": self._summary(),
        }

    def finish(self, write: bool) -> dict[str, Any]:
        if self.finished.is_set():
            return {"outcome": self.outcome, "summary": self._summary()}
        written: list[Path] = []
        if write:
            written = self.state.write()
            self.outcome = "written"
        else:
            self.outcome = "discarded"
        self.finished.set()
        return {
            "outcome": self.outcome,
            "written": [self.state.relative(path) for path in written],
            "summary": self._summary(),
        }


class ReviewHTTPServer(HTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        session: WebReviewSession,
        assets_dir: Path,
        token: str,
    ) -> None:
        super().__init__(address, ReviewRequestHandler)
        self.session = session
        self.assets_dir = assets_dir
        self.token = token


class ReviewRequestHandler(BaseHTTPRequestHandler):
    server: ReviewHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-Review-Token", ""), self.server.token
        )

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ReviewError("invalid content length") from error
        if length > 10 * 1024 * 1024:
            raise ReviewError("request is too large")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as error:
            raise ReviewError("invalid JSON body") from error
        if not isinstance(payload, dict):
            raise ReviewError("JSON body must be an object")
        return payload

    def _asset(self, request_path: str) -> None:
        immutable = request_path.startswith("/monaco/")
        if immutable:
            root = (self.server.assets_dir / "monaco").resolve()
            relative = request_path.removeprefix("/monaco/")
            if not relative:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            path = (root / relative).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            name = relative
        else:
            name = WEB_ASSETS.get(request_path)
            if name is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            path = self.server.assets_dir / name
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "web UI asset missing")
            return
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type = f"{content_type}; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Cache-Control",
            "public, max-age=31536000, immutable" if immutable else "no-store",
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        request_path = urlparse(self.path).path
        if request_path in WEB_ASSETS or request_path.startswith("/monaco/"):
            self._asset(request_path)
            return
        if not self._authorized():
            self._json({"error": "unauthorized"}, HTTPStatus.FORBIDDEN)
            return
        try:
            if request_path == "/api/state":
                self._json(self.server.session.snapshot())
            elif request_path == "/api/final-diff":
                self._json(self.server.session.final_diff())
            elif request_path.startswith("/api/findings/"):
                self._json(self.server.session.detail(int(request_path.rsplit("/", 1)[1])))
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ReviewError, ValueError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        request_path = urlparse(self.path).path
        if not self._authorized():
            self._json({"error": "unauthorized"}, HTTPStatus.FORBIDDEN)
            return
        try:
            payload = self._body()
            if request_path.startswith("/api/findings/"):
                finding_id = int(request_path.rsplit("/", 1)[1])
                edits = payload.get("edits")
                if edits is not None and not isinstance(edits, dict):
                    raise ReviewError("edits must be a file-to-contents object")
                self._json(self.server.session.decide(
                    finding_id, str(payload.get("decision", "")), edits
                ))
            elif request_path == "/api/finish":
                write = payload.get("write")
                if not isinstance(write, bool):
                    raise ReviewError("write must be true or false")
                self._json(self.server.session.finish(write))
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (OSError, ReviewError, ValueError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


def review_web(
    findings: list[Finding],
    state: ReviewState,
    ignored: int,
    port: int = 0,
    open_browser: bool = True,
) -> int:
    session = WebReviewSession(findings, state, ignored)
    token = secrets.token_urlsafe(24)
    server = ReviewHTTPServer(
        ("127.0.0.1", port), session, Path(__file__).resolve().parent, token
    )
    server.timeout = 0.5
    host, actual_port = server.server_address
    url = f"http://{host}:{actual_port}/#token={token}"
    print(f"\nWeb reviewer: {url}")
    print("Source files stay unchanged until you confirm Write changes in the browser.")
    print("Press Ctrl-C to stop and discard queued changes.")
    if open_browser:
        threading.Timer(0.15, webbrowser.open, args=(url,)).start()
    try:
        while not session.finished.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        print("\nReview stopped. No files changed.")
        return 130
    finally:
        server.server_close()
    if session.outcome == "written":
        print(f"Updated {len(state.changed())} file(s) from the web review.")
    else:
        print("Discarded queued changes. No files changed.")
    return session.result


def review(findings: list[Finding], state: ReviewState, ignored: int) -> int:
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    accepted_count = 0
    rejected_count = 0
    deferred_count = 0
    invalidated_count = 0
    invalidated_files: set[Path] = set()
    discard_all = False

    print(f"\nFound {len(findings)} reviewable diagnostic(s).")
    if ignored:
        print(f"Ignored {ignored} diagnostic(s) outside the selected library or generated code.")
    print("No source files have been changed. Empty input rejects a suggestion.")

    for index, finding in enumerate(findings, 1):
        # Do this before displaying the diagnostic or asking for input. A manual
        # full-file edit may already have fixed it, and its original message and
        # byte offsets cannot be trusted anymore.
        invalidated = state.invalidated_paths(finding)
        if invalidated:
            invalidated_count += 1
            invalidated_files.update(invalidated)
            continue

        print("\n" + "=" * 78)
        location = state.relative(finding.path) if finding.path else "unknown"
        print(f"[{index}/{len(findings)}] {finding.name}")
        print(f"{location}: {finding.message}")

        try:
            candidate = state.candidate(finding) if finding.replacements else {}
        except ReviewError as error:
            print(f"Deferred: {error}")
            deferred_count += 1
            continue

        if candidate:
            print(unified_diff(state.current, candidate, state.display_root, color))
        else:
            print("No automatic fix is available.")
            context = diagnostic_context(finding, state)
            if context:
                print(context)

        manually_changed = False
        while True:
            choice = prompt(
                "[a]ccept  [r]eject  [e]dit proposed file  "
                "[b]efore  [p]roposed  [d]iff  [q]uit  [x]discard all  [?]help: "
            )
            if choice in {"", "r"}:
                rejected_count += 1
                break
            if choice == "a":
                if not candidate:
                    print("There is no proposed change to accept; use 'e' to create one.")
                    continue
                state.accept(finding, candidate, manually_changed)
                accepted_count += 1
                if manually_changed:
                    paths = ", ".join(
                        sorted(state.relative(path) for path in candidate)
                    )
                    print(
                        f"Accepted manual edit for {paths}. Later diagnostics touching "
                        "the edited file(s) will be skipped until clang-tidy is rerun."
                    )
                break
            if choice == "e":
                edit_base = candidate
                if not edit_base and finding.path is not None:
                    edit_base = {finding.path: state.current[finding.path]}
                if not edit_base:
                    print("No in-scope file is available to edit.")
                    continue
                try:
                    candidate = edit_candidate(edit_base)
                    manually_changed = True
                    diff = unified_diff(state.current, candidate, state.display_root, color)
                    print(diff or "The editor made no changes.")
                except ReviewError as error:
                    print(f"Editor error: {error}")
                continue
            if choice == "b":
                paths = candidate or ({finding.path: b""} if finding.path else {})
                for path in paths:
                    print(numbered_file(path, state.current[path], "BEFORE"))
                continue
            if choice == "p":
                if not candidate:
                    print("No proposed file version is available; use 'e' to create one.")
                for path, data in candidate.items():
                    print(numbered_file(path, data, "PROPOSED"))
                continue
            if choice == "d":
                if candidate:
                    print(
                        unified_diff(state.current, candidate, state.display_root, color)
                        or "No difference."
                    )
                else:
                    print("No automatic fix is available.")
                continue
            if choice == "q":
                deferred_count += len(findings) - index + 1
                break
            if choice == "x":
                discard_all = True
                break
            if choice in {"?", "h", "help"}:
                print(
                    "Accept queues this diagnostic's complete replacement set. Reject leaves it "
                    "unchanged. Edit opens the proposed full file in $VISUAL/$EDITOR; after editing, "
                    "choose accept to queue your version. Later diagnostics touching a manually "
                    "edited file are skipped because their snapshot is stale. Quit proceeds to the "
                    "final write prompt. Discard all exits without writing queued changes."
                )
                continue
            print("Unknown choice.")

        if choice in {"q", "x"}:
            break

    if discard_all:
        print("Discarded all queued changes. No files changed.")
        return 0

    changed = state.changed()
    print("\n" + "=" * 78)
    print(
        f"Review summary: {accepted_count} accepted, {rejected_count} rejected, "
        f"{deferred_count} deferred, {invalidated_count} stale after manual edits."
    )
    if invalidated_count:
        labels = ", ".join(
            sorted(state.relative(path) for path in invalidated_files)
        )
        print(
            f"Skipped stale diagnostics for: {labels}. Rerun clang-tidy after writing "
            "to review any issues that remain."
        )
    if not changed:
        print("No changes were queued; no files changed.")
        return 0

    print("\nCombined queued diff:")
    print(unified_diff(state.original, state.current, state.display_root, color))
    if prompt(f"Write changes to {len(changed)} file(s)? [y/N]: ") not in {"y", "yes"}:
        print("Discarded queued changes. No files changed.")
        return 0

    try:
        written = state.write()
    except ReviewError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print("Updated:")
    for path in written:
        print(f"  {state.relative(path)}")
    print("Rerun the reviewer: later diagnostics may change after accepted or edited fixes.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixes_file", type=Path, help="YAML file produced by clang-tidy -export-fixes")
    parser.add_argument("--library-root", required=True, type=Path)
    parser.add_argument(
        "--display-root",
        type=Path,
        help="root used for displayed paths (default: inferred repository root)",
    )
    parser.add_argument(
        "--ui",
        choices=("web", "tui"),
        default="web",
        help="review interface (default: web)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="print the web reviewer URL without opening a browser",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="web reviewer port (default: choose an available port)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    library_root = args.library_root.resolve()
    display_root = args.display_root.resolve() if args.display_root else None
    try:
        findings, ignored = load_findings(args.fixes_file, library_root)
        if not findings:
            if ignored:
                print(
                    f"clang-tidy exported {ignored} diagnostic(s), but all were outside "
                    f"the review scope {library_root}. No files changed."
                )
            else:
                print("clang-tidy exported no in-scope diagnostics or fixes. No files changed.")
            return 0
        state = ReviewState(findings, library_root, display_root)
        if args.ui == "tui":
            return review(findings, state, ignored)
        if not 0 <= args.port <= 65535:
            raise ReviewError("port must be between 0 and 65535")
        return review_web(
            findings, state, ignored, port=args.port, open_browser=not args.no_open
        )
    except (OSError, ReviewError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
