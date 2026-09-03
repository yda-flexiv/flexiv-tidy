#!/usr/bin/env bash

# Fast, container-side clang-tidy diagnostics using lljbash/clangd-tidy.
# The standalone clang-tidy target remains available as the authoritative check
# because clangd deliberately disables checks that are unsafe or too slow in an LSP.

set -euo pipefail

PROJECT_ROOT="${FLEXIV_TIDY_PROJECT:-$(pwd)}"
readonly PROJECT_ROOT
readonly CLANG_TIDY_CONFIG_FILE="${FLEXIV_TIDY_CONFIG:-$PROJECT_ROOT/cmake/tools/.clang-tidy}"

usage() {
    cat <<'EOF'
Usage: ./run_clangd_tidy_check.sh [-j JOBS] [--batch-size FILES]
                                  [-p COMPILE_COMMANDS_DIR] [PATH ...]

Run fast clang-tidy diagnostics through one persistent clangd process. PATH may
name a source file or directory; only translation units present in the compile
database are selected. PATH defaults to lib/.

Environment overrides:
  CLANGD_TIDY_JOBS          clangd worker count (default: min(container CPUs, 10))
  CLANGD_TIDY_BATCH_SIZE    files per clangd process (default: 25)
  CLANGD_TIDY_CLANGD        clangd executable (default: clangd or clangd-N)
  CLANGD_TIDY_QUERY_DRIVER  optional compiler executable or glob trusted by clangd
EOF
}

default_jobs="$(nproc)"
if ((default_jobs > 10)); then
    default_jobs=10
fi
jobs="${CLANGD_TIDY_JOBS:-$default_jobs}"
batch_size="${CLANGD_TIDY_BATCH_SIZE:-25}"
compile_commands_dir=""
targets=()

while (($#)); do
    case "$1" in
        -j|--jobs)
            [[ $# -ge 2 ]] || { echo "Error: $1 requires a value." >&2; exit 2; }
            jobs="$2"
            shift 2
            ;;
        -p|--compile-commands-dir)
            [[ $# -ge 2 ]] || { echo "Error: $1 requires a value." >&2; exit 2; }
            compile_commands_dir="$2"
            shift 2
            ;;
        --batch-size)
            [[ $# -ge 2 ]] || { echo "Error: $1 requires a value." >&2; exit 2; }
            batch_size="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            targets+=("$@")
            break
            ;;
        -*)
            echo "Error: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            targets+=("$1")
            shift
            ;;
    esac
done

[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || {
    echo "Error: jobs must be a positive integer, got '$jobs'." >&2
    exit 2
}
[[ "$batch_size" =~ ^[1-9][0-9]*$ ]] || {
    echo "Error: batch size must be a positive integer, got '$batch_size'." >&2
    exit 2
}

if [[ ! -f /.dockerenv && ! -d /opt/flexiv_thirdparty2 ]]; then
    echo "Error: run clangd-tidy inside the Docker build environment." >&2
    echo "Use: ./shell-docker.sh './run_clangd_tidy_check.sh [PATH ...]'" >&2
    exit 1
fi

command -v python3 >/dev/null 2>&1 || {
    echo "Error: python3 is required." >&2
    exit 1
}
clangd_executable="${CLANGD_TIDY_CLANGD:-}"
if [[ -z "$clangd_executable" ]]; then
    for candidate in clangd clangd-{22..15}; do
        if command -v "$candidate" >/dev/null 2>&1; then
            clangd_executable="$(command -v "$candidate")"
            break
        fi
    done
fi
[[ -n "$clangd_executable" && -x "$clangd_executable" ]] || {
    echo "Error: clangd is required (set CLANGD_TIDY_CLANGD if it is versioned)." >&2
    exit 1
}

runner=()
if command -v clangd-tidy >/dev/null 2>&1; then
    runner=(clangd-tidy)
else
    echo "Error: clangd-tidy is not installed in Docker." >&2
    echo "Run: bash install_clangd_tidy_in_docker.sh" >&2
    exit 1
fi

cd "$PROJECT_ROOT"

if [[ ! -f "$CLANG_TIDY_CONFIG_FILE" ]]; then
    echo "Error: clang-tidy config not found: $CLANG_TIDY_CONFIG_FILE" >&2
    exit 1
fi

if [[ -n "$compile_commands_dir" ]]; then
    compile_commands_dir="$(cd "$compile_commands_dir" && pwd)"
    canonical_db="$compile_commands_dir/compile_commands.json"
else
    canonical_db=""
    for candidate in \
        "$PROJECT_ROOT/build/clang-tidy/compile_commands.json" \
        "$PROJECT_ROOT/build/Linux/RelWithDebInfo/compile_commands.json" \
        "$PROJECT_ROOT/compile_commands.json"; do
        if [[ -f "$candidate" ]]; then
            canonical_db="$candidate"
            break
        fi
    done
fi

if [[ -z "$canonical_db" || ! -f "$canonical_db" ]]; then
    echo "Error: compile_commands.json was not found." >&2
    echo "Configure first with: ./shell-docker.sh 'cmake --preset ubuntu-static-check'" >&2
    exit 1
fi

# Keep the canonical /opt/flexiv_thirdparty2 paths inside Docker.
clangd_db="$canonical_db"

if ((${#targets[@]} == 0)); then
    targets=(lib)
fi

source_files=()
mapfile -d '' -t source_files < <(
    python3 - "$clangd_db" "${targets[@]}" <<'PY'
import json
import pathlib
import sys

db_path = pathlib.Path(sys.argv[1]).resolve()
root = pathlib.Path.cwd().resolve()
requested = []
for value in sys.argv[2:]:
    path = pathlib.Path(value)
    requested.append((root / path).resolve() if not path.is_absolute() else path.resolve())

with db_path.open(encoding="utf-8") as stream:
    commands = json.load(stream)

files = set()
for entry in commands:
    path = pathlib.Path(entry["file"])
    if not path.is_absolute():
        path = pathlib.Path(entry.get("directory", root)) / path
    path = path.resolve()
    # Skip stale compile-database entries whose source file no longer exists.
    if not path.is_file():
        continue
    for target in requested:
        try:
            if path == target or (target.is_dir() and path.is_relative_to(target)):
                files.add(path)
                break
        except AttributeError:
            try:
                path.relative_to(target)
                files.add(path)
                break
            except ValueError:
                pass

for path in sorted(files):
    sys.stdout.buffer.write(str(path).encode() + b"\0")
PY
)

if ((${#source_files[@]} == 0)); then
    echo "Error: no translation units from the compile database matched: ${targets[*]}" >&2
    exit 1
fi

# clangd discovers .clang-tidy by walking upward from each source file and has
# no command-line option for an arbitrary config path. Create a disposable
# mirror containing symlinks to the canonical config and hard-linked sources,
# then rewrite a minimal compile database to address the mirrored paths. This
# keeps cmake/tools/.clang-tidy as the sole project configuration file.
analysis_dir="$(mktemp -d "${TMPDIR:-/tmp}/flexiv-clangd-tidy.XXXXXX")"
trap 'rm -rf -- "$analysis_dir"' EXIT
ln -s "$CLANG_TIDY_CONFIG_FILE" "$analysis_dir/.clang-tidy"

analysis_files=()
mapfile -d '' -t analysis_files < <(
    python3 - "$clangd_db" "$PROJECT_ROOT" "$analysis_dir" "${source_files[@]}" <<'PY'
import json
import os
import pathlib
import shlex
import shutil
import sys

db_path = pathlib.Path(sys.argv[1]).resolve()
root = pathlib.Path(sys.argv[2]).resolve()
analysis_root = pathlib.Path(sys.argv[3]).resolve()
source_paths = [pathlib.Path(value).resolve() for value in sys.argv[4:]]

mirrored = {}
for source in source_paths:
    try:
        relative = source.relative_to(root)
    except ValueError as error:
        raise SystemExit(f"source is outside the project root: {source}") from error
    destination = analysis_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    mirrored[source] = destination

with db_path.open(encoding="utf-8") as stream:
    commands = json.load(stream)

analysis_commands = []
for entry in commands:
    entry_file = pathlib.Path(entry["file"])
    if not entry_file.is_absolute():
        entry_file = pathlib.Path(entry.get("directory", root)) / entry_file
    source = entry_file.resolve()
    destination = mirrored.get(source)
    if destination is None:
        continue

    rewritten = dict(entry)
    rewritten["file"] = str(destination)
    directory = pathlib.Path(entry.get("directory", root))

    def rewrite_argument(argument):
        if argument == entry["file"]:
            return str(destination)
        candidate = pathlib.Path(argument)
        if candidate == source:
            return str(destination)
        if not candidate.is_absolute() and (directory / candidate).resolve() == source:
            return str(destination)
        return argument

    if "arguments" in rewritten:
        rewritten["arguments"] = [rewrite_argument(value) for value in rewritten["arguments"]]
    elif "command" in rewritten:
        rewritten["command"] = shlex.join(
            rewrite_argument(value) for value in shlex.split(rewritten["command"])
        )
    analysis_commands.append(rewritten)

if len(analysis_commands) != len(mirrored):
    raise SystemExit(
        f"compile database contains {len(analysis_commands)} of "
        f"{len(mirrored)} selected translation units"
    )

with (analysis_root / "compile_commands.json").open("w", encoding="utf-8") as stream:
    json.dump(analysis_commands, stream)

for source in source_paths:
    sys.stdout.buffer.write(str(mirrored[source]).encode() + b"\0")
PY
)
compile_commands_dir="$analysis_dir"

query_driver_args=()
if [[ -n "${CLANGD_TIDY_QUERY_DRIVER:-}" ]]; then
    query_driver_args=(--query-driver "$CLANGD_TIDY_QUERY_DRIVER")
fi

echo "clangd-tidy: ${#source_files[@]} translation unit(s), $jobs worker(s)"
echo "compile database: $clangd_db"
echo "clang-tidy config: $CLANG_TIDY_CONFIG_FILE"
echo "query driver: ${CLANGD_TIDY_QUERY_DRIVER:-clangd default}"

batch_count=$(((${#analysis_files[@]} + batch_size - 1) / batch_size))
exit_status=0
for ((batch_index = 0; batch_index < batch_count; ++batch_index)); do
    offset=$((batch_index * batch_size))
    batch=("${analysis_files[@]:offset:batch_size}")
    echo "batch $((batch_index + 1))/$batch_count: ${#batch[@]} translation unit(s)"
    if "${runner[@]}" \
        -p "$compile_commands_dir" \
        -j "$jobs" \
        --clangd-executable "$clangd_executable" \
        "${query_driver_args[@]}" \
        --fail-on-severity error \
        --compact \
        "${batch[@]}"; then
        :
    else
        exit_status=$?
    fi
done

exit "$exit_status"
