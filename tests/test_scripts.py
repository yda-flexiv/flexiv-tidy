from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from flexiv_tidy import assets_dir


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tidy scripts ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "lib/base/Example/example.cpp"
        self.source.parent.mkdir(parents=True)
        self.source.write_text('#include "sibling.h"\nint value = ANSWER;\n')
        (self.source.parent / "sibling.h").write_text("#define ANSWER 42\n")
        (self.source.parent / "CMakeLists.txt").touch()
        self.build = self.root / "build/clang-tidy"
        self.build.mkdir(parents=True)
        self.entry = {
            "directory": str(self.root),
            "file": str(self.source),
            "arguments": ["c++", "-fsyntax-only", str(self.source)],
        }
        self.database = self.build / "compile_commands.json"
        self.database.write_text(json.dumps([self.entry]))
        self.config = self.root / "cmake/tools/.clang-tidy"
        self.config.parent.mkdir(parents=True)
        self.config.write_text("Checks: '*'\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls.jsonl"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FLEXIV_TIDY_PROJECT": str(self.root),
            "FLEXIV_TIDY_CONFIG": str(self.config),
            "CLANGD_TIDY_CLANGD": shutil.which("true"),
            "CLANGD_TIDY_JOBS": "1",
            "CLANGD_TIDY_BATCH_SIZE": "25",
            "TEST_CALLS": str(self.calls),
            "TEST_STATUS": "0",
            "TEST_COMPILE": "0",
            "TMPDIR": str(self.root),
        }

    def executable(self, path, contents):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        path.chmod(0o755)

    def clangd(self, *args):
        # Exercise the real orchestration with stub executables on the host;
        # only bypass the environment guard, since this suite needs no Docker.
        script = (assets_dir() / "run_clangd_tidy_check.sh").read_text()
        script = script.replace(
            '[[ ! -f /.dockerenv && ! -d /opt/flexiv_thirdparty2 ]]', "false"
        )
        runner = self.root / "runner.sh"
        runner.write_text(script)
        self.executable(self.bin / "clangd-tidy", """#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
database = pathlib.Path(args[args.index('-p') + 1]) / 'compile_commands.json'
commands = json.loads(database.read_text())
with open(os.environ['TEST_CALLS'], 'a') as stream:
    stream.write(json.dumps({'args': args, 'commands': commands}) + '\\n')
if os.environ['TEST_COMPILE'] == '1':
    for command in commands:
        subprocess.run(command['arguments'], cwd=command['directory'], check=True)
sys.exit(int(os.environ['TEST_STATUS']))
""")
        return subprocess.run(
            ["bash", str(runner), *args], cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=15,
        )

    def test_duplicate_database_entries_are_analyzed(self):
        self.database.write_text(json.dumps([self.entry, self.entry]))
        result = self.clangd()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]["commands"]), 2)
        self.assertIn("batch 1/1", result.stdout)

    def test_mirror_preparation_failure_returns_failure(self):
        with tempfile.TemporaryDirectory() as outside:
            source = Path(outside) / "outside.cpp"
            source.touch()
            self.database.write_text(json.dumps([{**self.entry, "file": str(source)}]))
            result = self.clangd(str(source))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("outside the project root", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertEqual(list(self.root.glob("flexiv-clangd-tidy.*")), [])

    def test_invalid_database_returns_failure(self):
        self.database.write_text("invalid json")
        result = self.clangd()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_diagnostic_failure_is_propagated(self):
        self.env["TEST_STATUS"] = "7"
        result = self.clangd()
        self.assertEqual(result.returncode, 7, result.stderr)

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_mirrored_source_can_include_sibling_header(self):
        self.env["TEST_COMPILE"] = "1"
        for use_command in (False, True):
            with self.subTest(use_command=use_command):
                entry = dict(self.entry)
                if use_command:
                    import shlex
                    entry["command"] = shlex.join(entry.pop("arguments"))
                self.database.write_text(json.dumps([entry]))
                result = self.clangd()
                self.assertEqual(result.returncode, 0, result.stderr)

    def fix(self, *args):
        self.executable(self.root / "docker/docker_dispatch.sh", """#!/usr/bin/env python3
import json, os, sys
with open(os.environ['TEST_CALLS'], 'a') as stream:
    stream.write(json.dumps(sys.argv[6:]) + '\\n')
""")
        return subprocess.run(
            ["bash", str(assets_dir() / "fix_clang_tidy.sh"), *args],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15,
        )

    def test_fix_accepts_flat_and_nested_libraries(self):
        flat = self.root / "lib/FlatExample"
        shutil.copytree(self.source.parent, flat)
        flat_source = flat / self.source.name
        self.database.write_text(json.dumps([
            self.entry, {**self.entry, "file": str(flat_source)},
        ]))
        for source in (self.source, flat_source):
            library = source.parent
            for spec in (
                library.name, str(library.relative_to(self.root / "lib")),
                str(library.relative_to(self.root)), str(library) + "/",
            ):
                with self.subTest(spec=spec):
                    result = self.fix("--dry-run", "--library-only", spec)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    args = json.loads(self.calls.read_text().splitlines()[-1])
                    selected = {p for p in (self.source, flat_source)
                                if re.fullmatch(args[5], str(p))}
                    self.assertEqual(selected, {source})
                    filtered = {item["name"] for item in json.loads(args[3])}
                    self.assertEqual(filtered, {str(source), str(library / "sibling.h")})
                    self.assertTrue(all("lines" not in item for item in json.loads(args[3])))

    def test_fix_compacts_large_dependency_filter(self):
        dependency = self.root / "lib/Other"
        dependency.mkdir()
        for index in range(940):
            (dependency / f"{index:04d}_{'x' * 75}.h").touch()

        result = self.fix("--dry-run", "Example")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.calls.read_text().splitlines()[-1])
        line_filter = args[3]
        self.assertLess(len(line_filter.encode()), 120_000)
        names = {item["name"] for item in json.loads(line_filter)}
        self.assertTrue(str(self.source.relative_to(self.root.parent)) in names)
        self.assertTrue(
            str((dependency / f"0939_{'x' * 75}.h").relative_to(self.root.parent))
            in names
        )
        self.assertEqual(len(names), 942)

    def test_fix_includes_external_headers_without_unbounded_line_filter(self):
        external = self.root / "external/flexiv_sw_base/lib/FvrSystemUpdate/Inc"
        external.mkdir(parents=True)
        header = external / "CommSystemUpdateState.hpp"
        header.write_text("class CommSystemUpdateStatePub {};\n")
        generated = external / "Generated"
        generated.mkdir()
        (generated / "skip.hpp").touch()

        result = self.fix("--dry-run", "Example")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.calls.read_text().splitlines()[-1])
        self.assertEqual(args[3], "")
        self.assertIsNotNone(re.search(args[2], str(header)))
        self.assertIsNone(re.search(args[2], str(generated / "skip.hpp")))

        self.calls.unlink()
        result = self.fix("--dry-run", "--library-only", "Example")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.calls.read_text().splitlines()[-1])
        self.assertIsNone(re.search(args[2], str(header)))
        self.assertNotIn(str(header), {item["name"] for item in json.loads(args[3])})

        self.calls.unlink()
        result = self.fix("--apply-all", "Example")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--apply-all with external dependencies is unsafe", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_fix_short_name_ignores_non_library_directories(self):
        (self.root / "lib/Example").mkdir()
        result = self.fix("--dry-run", "Example")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fix_rejects_ambiguous_flat_and_nested_short_name(self):
        flat = self.root / "lib/Example"
        flat.mkdir()
        (flat / "CMakeLists.txt").touch()
        result = self.fix("--dry-run", "Example")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ambiguous", result.stderr)
        self.assertIn("base/Example", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_fix_rejects_invalid_library_paths(self):
        for relative in ("lib/base/Example/deeper", "outside", "lib/NoCMake"):
            path = self.root / relative
            path.mkdir()
            if path.name != "NoCMake":
                (path / "CMakeLists.txt").touch()
        (self.root / "lib/Escape").symlink_to(self.root / "outside")
        for spec in ("lib/base/Example/deeper", "lib/../outside", "lib/Escape",
                     "lib/NoCMake", "Missing"):
            with self.subTest(spec=spec):
                result = self.fix("--dry-run", spec)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_apply_all_rechecks_using_bundled_script(self):
        self.executable(self.root / "docker/docker_dispatch.sh", """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[6:]
with open(os.environ['TEST_CALLS'], 'a') as stream:
    stream.write(json.dumps({'mode': args[6], 'config': args[1]}) + '\\n')
sys.exit(42 if args[6] == 'dry-run' else 0)
""")
        result = subprocess.run(
            ["bash", str(assets_dir() / "fix_clang_tidy.sh"),
             "--apply-all", "--library-only", "Example"],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([call["mode"] for call in calls], ["apply-all", "dry-run"])
        self.assertIn("Some diagnostics remain", result.stderr)


if __name__ == "__main__":
    unittest.main()
