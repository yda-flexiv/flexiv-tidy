from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REVIEWER_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "flexiv_tidy"
    / "assets"
    / "review_clang_tidy_fixes.py"
)
SPEC = importlib.util.spec_from_file_location("flexiv_tidy_web_reviewer", REVIEWER_PATH)
assert SPEC and SPEC.loader
reviewer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reviewer
SPEC.loader.exec_module(reviewer)


class WebReviewSessionTest(unittest.TestCase):
    def test_monaco_assets_are_bundled(self):
        assets = REVIEWER_PATH.parent
        self.assertTrue((assets / "monaco" / "vs" / "loader.js").is_file())
        self.assertTrue((assets / "monaco" / "vs" / "editor" / "editor.main.js").is_file())
        self.assertTrue((assets / "monaco" / "LICENSE").is_file())

    def make_review(self, temporary: str):
        library_root = Path(temporary) / "repo" / "lib" / "comm" / "Example"
        library_root.mkdir(parents=True)
        source = library_root / "example.cpp"
        source.write_bytes(b"bad();\nbad();\n")
        findings = [
            reviewer.Finding(
                "modernize-example",
                "replace bad with good",
                source,
                offset,
                (reviewer.Replacement(source, offset, 3, b"good"),),
            )
            for offset in (0, 7)
        ]
        state = reviewer.ReviewState(findings, library_root)
        return source, reviewer.WebReviewSession(findings, state, ignored=2)

    def test_accept_is_queued_until_final_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, session = self.make_review(temporary)
            snapshot = session.decide(1, "accept")

            self.assertEqual(source.read_bytes(), b"bad();\nbad();\n")
            self.assertEqual(snapshot["summary"]["accepted"], 1)
            self.assertEqual(snapshot["summary"]["changed_files"], 1)

            result = session.finish(write=True)
            self.assertEqual(result["outcome"], "written")
            self.assertEqual(source.read_bytes(), b"good();\nbad();\n")

    def test_manual_accept_marks_later_finding_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, session = self.make_review(temporary)
            label = session.detail(1)["files"][0]["path"]

            snapshot = session.decide(
                1, "accept", edits={label: "good();\ngood();\n"}
            )

            self.assertEqual(snapshot["findings"][1]["status"], "stale")
            self.assertEqual(source.read_bytes(), b"bad();\nbad();\n")

    def test_discard_leaves_source_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, session = self.make_review(temporary)
            session.decide(1, "accept")
            result = session.finish(write=False)

            self.assertEqual(result["outcome"], "discarded")
            self.assertEqual(source.read_bytes(), b"bad();\nbad();\n")


if __name__ == "__main__":
    unittest.main()
