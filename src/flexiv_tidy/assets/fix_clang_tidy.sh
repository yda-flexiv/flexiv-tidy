#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./fix_clang_tidy.sh [options] <library>

Run clang-tidy on one library's translation units and review its fixes.

The library may be a unique short name (FvrRemoteParam), a path relative to
lib/ (comm/FvrRemoteParam), or a repository path (lib/comm/FvrRemoteParam).

Options:
  -n, --dry-run              Report diagnostics without opening the reviewer
      --apply-all            Apply all available fixes without review (risky)
      --include-dependencies Include diagnostics/fixes in dependencies under lib/
      --library-only         Only report/fix files inside the selected library
  -j, --jobs <count>         Parallel clang-tidy jobs (default: CPU count)
      --build-dir <dir>      Compilation database directory (default: build/clang-tidy)
      --tui                  Use the terminal reviewer instead of the web UI
      --no-open               Print Web UI URL without opening a browser
      --web-port <port>       Bind the local Web UI to a specific port
  -h, --help                 Show this help

Scope defaults:
  All modes include in-tree dependency headers exposed by the selected library,
  matching run_clang_tidy_check.sh. Use --library-only for a strict write scope.

Examples:
  ./fix_clang_tidy.sh FvrRemoteParam
  ./fix_clang_tidy.sh --library-only comm/FvrRemoteParam
  ./fix_clang_tidy.sh --dry-run FvrRemoteParam
  ./fix_clang_tidy.sh --apply-all --library-only base/FvrBase
EOF
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

REPO_ROOT="${FLEXIV_TIDY_PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PYTHON="${FLEXIV_TIDY_PYTHON:-python3}"
BUILD_DIR="${CLANG_TIDY_BUILD_DIR:-$REPO_ROOT/build/clang-tidy}"
CONFIG_FILE="${FLEXIV_TIDY_CONFIG:-$REPO_ROOT/cmake/tools/.clang-tidy}"
REVIEWER="${FLEXIV_TIDY_REVIEWER:-$REPO_ROOT/review_clang_tidy_fixes.py}"
MODE="review"
SCOPE_MODE="auto"
JOBS=0
LIBRARY_SPEC=""
REVIEW_UI="${FLEXIV_TIDY_UI:-web}"
REVIEW_OPEN=true
WEB_PORT=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        -n|--dry-run)
            [ "$MODE" = "review" ] || fail "--dry-run and --apply-all cannot be combined"
            MODE="dry-run"
            shift
            ;;
        --apply-all)
            [ "$MODE" = "review" ] || fail "--dry-run and --apply-all cannot be combined"
            MODE="apply-all"
            shift
            ;;
        --include-dependencies)
            [ "$SCOPE_MODE" = "auto" ] || fail "scope options cannot be combined"
            SCOPE_MODE="dependencies"
            shift
            ;;
        --library-only)
            [ "$SCOPE_MODE" = "auto" ] || fail "scope options cannot be combined"
            SCOPE_MODE="library"
            shift
            ;;
        -j|--jobs)
            [ "$#" -ge 2 ] || fail "$1 requires a value"
            JOBS="$2"
            shift 2
            ;;
        --build-dir)
            [ "$#" -ge 2 ] || fail "$1 requires a value"
            BUILD_DIR="$2"
            shift 2
            ;;
        --tui)
            REVIEW_UI="tui"
            shift
            ;;
        --no-open)
            REVIEW_OPEN=false
            shift
            ;;
        --web-port)
            [ "$#" -ge 2 ] || fail "$1 requires a value"
            WEB_PORT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            [ "$#" -eq 1 ] || fail "exactly one library must be specified"
            LIBRARY_SPEC="$1"
            shift
            ;;
        -*)
            fail "unknown option: $1"
            ;;
        *)
            [ -z "$LIBRARY_SPEC" ] || fail "exactly one library must be specified"
            LIBRARY_SPEC="$1"
            shift
            ;;
    esac
done

[ -n "$LIBRARY_SPEC" ] || { usage >&2; exit 2; }
[[ "$JOBS" =~ ^[0-9]+$ ]] || fail "job count must be a non-negative integer"
[[ "$WEB_PORT" =~ ^[0-9]+$ ]] && [ "$WEB_PORT" -le 65535 ] \
    || fail "web port must be an integer between 0 and 65535"
[[ "$REVIEW_UI" == "web" || "$REVIEW_UI" == "tui" ]] \
    || fail "FLEXIV_TIDY_UI must be 'web' or 'tui'"

if [ "$SCOPE_MODE" = "auto" ]; then
    SCOPE_MODE="dependencies"
fi

resolve_library() {
    local spec="${1%/}"
    local candidate
    local -a matches=()

    case "$spec" in
        "$REPO_ROOT"/lib/*) candidate="$spec" ;;
        lib/*) candidate="$REPO_ROOT/$spec" ;;
        */*) candidate="$REPO_ROOT/lib/$spec" ;;
        *)
            while IFS= read -r -d '' candidate; do
                matches+=("$candidate")
            done < <(find "$REPO_ROOT/lib" -mindepth 2 -maxdepth 2 -type d -name "$spec" -print0)
            if [ "${#matches[@]}" -eq 0 ]; then
                fail "no library named '$spec' exists under lib/"
            elif [ "${#matches[@]}" -gt 1 ]; then
                echo "Error: library name '$spec' is ambiguous; use one of:" >&2
                printf '  %s\n' "${matches[@]#"$REPO_ROOT/lib/"}" >&2
                exit 1
            fi
            candidate="${matches[0]}"
            ;;
    esac

    [ -d "$candidate" ] || fail "library directory does not exist: $candidate"
    candidate="$(realpath "$candidate")"
    local relative="${candidate#"$REPO_ROOT/lib/"}"
    if [ "$relative" = "$candidate" ] || [[ "$relative" != */* ]] \
        || [[ "${relative#*/}" == */* ]]; then
        fail "library must be a direct lib/<category>/<name> directory"
    fi
    [ -f "$candidate/CMakeLists.txt" ] || fail "library has no CMakeLists.txt: $candidate"
    printf '%s\n' "$candidate"
}

case "$BUILD_DIR" in
    /*) ;;
    *) BUILD_DIR="$REPO_ROOT/$BUILD_DIR" ;;
esac
BUILD_DIR="${BUILD_DIR%/}"
LIBRARY_DIR="$(resolve_library "$LIBRARY_SPEC")"
LIBRARY_REL="${LIBRARY_DIR#"$REPO_ROOT/lib/"}"
COMPILE_COMMANDS="$BUILD_DIR/compile_commands.json"

[ -f "$COMPILE_COMMANDS" ] || fail \
    "missing $COMPILE_COMMANDS; configure with: cmake --preset ubuntu-static-check -B build/clang-tidy"
[ -f "$CONFIG_FILE" ] || fail "clang-tidy configuration not found: $CONFIG_FILE"
[ "$MODE" != "review" ] || [ -f "$REVIEWER" ] || fail "reviewer not found: $REVIEWER"

# Only selected-library translation units are analyzed. Dependency mode widens
# where diagnostics/fixes may be reported, not which unrelated .cpp files run.
if ! TRANSLATION_UNIT_OUTPUT=$("$PYTHON" - "$COMPILE_COMMANDS" "$LIBRARY_DIR" <<'PY'
import json
import pathlib
import sys

database_path = pathlib.Path(sys.argv[1])
library_path = pathlib.Path(sys.argv[2]).resolve()
try:
    database = json.loads(database_path.read_text())
except (OSError, json.JSONDecodeError) as error:
    print(f"Error: cannot read compilation database: {error}", file=sys.stderr)
    raise SystemExit(1)

translation_units = set()
for entry in database:
    source = pathlib.Path(entry.get("file", ""))
    if not source.is_absolute():
        source = pathlib.Path(entry.get("directory", "")) / source
    source = source.resolve()
    try:
        relative = source.relative_to(library_path)
    except ValueError:
        continue
    if any("generated" in part.lower() for part in relative.parts):
        continue
    if source.is_file() and source.suffix.lower() in {".c", ".cc", ".cpp", ".cxx"}:
        translation_units.add(str(source))

print("\n".join(sorted(translation_units)))
PY
); then
    exit 1
fi

[ -n "$TRANSLATION_UNIT_OUTPUT" ] || fail \
    "no non-generated translation units for $LIBRARY_REL were found in $COMPILE_COMMANDS"
mapfile -t TRANSLATION_UNITS <<<"$TRANSLATION_UNIT_OUTPUT"

# run-clang-tidy positional arguments are regular expressions.
FILE_REGEX=$("$PYTHON" - "${TRANSLATION_UNITS[@]}" <<'PY'
import re
import sys
print("^(?:" + "|".join(re.escape(path) for path in sys.argv[1:]) + ")$")
PY
)

if [ "$SCOPE_MODE" = "dependencies" ]; then
    REVIEW_ROOT="$REPO_ROOT/lib"
    SCOPE_DESCRIPTION="selected library plus in-tree dependencies under lib/"
    LINE_FILTER_KIND="headers"
else
    REVIEW_ROOT="$LIBRARY_DIR"
    SCOPE_DESCRIPTION="selected library only"
    LINE_FILTER_KIND="all"
fi

# Keep the filter below Linux's per-argument limit. Dependency .cpp files are not
# analyzed; only the selected translation units and non-generated headers need to
# be listed. This remains a precise write boundary for --apply-all.
LINE_FILTER=$("$PYTHON" - "$REVIEW_ROOT" "$LINE_FILTER_KIND" "${TRANSLATION_UNITS[@]}" <<'PY'
import json
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
kind = sys.argv[2]
translation_units = {str(pathlib.Path(path).resolve()) for path in sys.argv[3:]}
header_extensions = {".h", ".hh", ".hpp", ".hxx", ".inc"}
source_extensions = {".c", ".cc", ".cpp", ".cxx"}
extensions = header_extensions if kind == "headers" else header_extensions | source_extensions
paths = set(translation_units)
for directory, directories, names in os.walk(root):
    directories[:] = sorted(
        item for item in directories if "generated" not in item.lower()
    )
    for name in sorted(names):
        path = pathlib.Path(directory, name)
        if path.suffix.lower() in extensions:
            paths.add(str(path.resolve()))

line_filter = json.dumps(
    [{"name": path, "lines": [[1, 2147483647]]} for path in sorted(paths)],
    separators=(",", ":"),
)
if len(line_filter.encode()) >= 120_000:
    print(
        "Error: clang-tidy line filter exceeds the safe command-line size; "
        "use --library-only or interactive review",
        file=sys.stderr,
    )
    raise SystemExit(1)
print(line_filter)
PY
)

HEADER_FILTER=$("$PYTHON" - "$REVIEW_ROOT" <<'PY'
import os
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1]).resolve()
header_extensions = {".h", ".hh", ".hpp", ".hxx", ".inc"}
directories = set()
for directory, child_directories, names in os.walk(root):
    child_directories[:] = [
        item for item in child_directories if "generated" not in item.lower()
    ]
    if any(pathlib.Path(name).suffix.lower() in header_extensions for name in names):
        directories.add(str(pathlib.Path(directory).resolve()))

# clang-tidy uses LLVM's POSIX regular expressions, so use a capturing group
# rather than Python's non-capturing (?:...) syntax.
if directories:
    print("^(" + "|".join(re.escape(path) for path in sorted(directories)) + ")/")
else:
    print("a^")
PY
)

echo "Analysis translation units: lib/$LIBRARY_REL"
echo "Diagnostic/fix scope: $SCOPE_DESCRIPTION"
echo "Compilation database: $COMPILE_COMMANDS"
echo "Configuration: $CONFIG_FILE"
echo "Selected ${#TRANSLATION_UNITS[@]} translation unit(s)."

DOCKER_DISPATCH="$REPO_ROOT/docker/docker_dispatch.sh"
[ -x "$DOCKER_DISPATCH" ] || fail "Docker dispatcher is not executable: $DOCKER_DISPATCH"

FIXES_FILE=""
if [ "$MODE" = "review" ]; then
    command -v "$PYTHON" >/dev/null 2>&1 || fail "python3 is required for interactive review"
    "$PYTHON" -c 'import yaml' 2>/dev/null || fail "Python package PyYAML is required"
    FIXES_FILE=$(mktemp "$BUILD_DIR/clang-tidy-review.XXXXXX.yaml")
    trap 'rm -f -- "$FIXES_FILE"' EXIT
    echo "Collecting suggestions without modifying files..."
elif [ "$MODE" = "dry-run" ]; then
    echo "Running clang-tidy without applying fixes..."
else
    echo "Applying all fixes in '$SCOPE_DESCRIPTION' without review..."
fi

set +e
"$DOCKER_DISPATCH" exec ubuntu bash -s -- \
    "$BUILD_DIR" "$CONFIG_FILE" "$HEADER_FILTER" "$LINE_FILTER" "$JOBS" \
    "$FILE_REGEX" "$MODE" "$FIXES_FILE" <<'INSIDE_DOCKER'
set -euo pipefail

build_dir="$1"
config_file="$2"
header_filter="$3"
line_filter="$4"
jobs="$5"
file_regex="$6"
mode="$7"
fixes_file="$8"

runner=""
clang_tidy=""
apply_replacements=""
for version in 22 21 20 19 18 17 16 15; do
    for candidate in "run-clang-tidy-$version" "run-clang-tidy-$version.py"; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && command -v "clang-tidy-$version" >/dev/null 2>&1 \
            && command -v "clang-apply-replacements-$version" >/dev/null 2>&1; then
            runner="$(command -v "$candidate")"
            clang_tidy="$(command -v "clang-tidy-$version")"
            apply_replacements="$(command -v "clang-apply-replacements-$version")"
            break 2
        fi
    done
done
if [ -z "$runner" ] \
    && command -v run-clang-tidy >/dev/null 2>&1 \
    && command -v clang-tidy >/dev/null 2>&1 \
    && command -v clang-apply-replacements >/dev/null 2>&1; then
    runner="$(command -v run-clang-tidy)"
    clang_tidy="$(command -v clang-tidy)"
    apply_replacements="$(command -v clang-apply-replacements)"
fi
[ -n "$runner" ] || {
    echo "Error: matching clang-tidy/run-clang-tidy tools (version 15+) were not found." >&2
    exit 1
}

arguments=(
    -quiet
    -p "$build_dir"
    -clang-tidy-binary "$clang_tidy"
    -clang-apply-replacements-binary "$apply_replacements"
    -config-file "$config_file"
    -header-filter "$header_filter"
)
if [ -n "$line_filter" ]; then
    arguments+=(-line-filter "$line_filter")
fi
if [ "$mode" = "apply-all" ]; then
    arguments+=(-fix -format -style file)
elif [ "$mode" = "review" ]; then
    arguments+=(-export-fixes "$fixes_file")
fi
if [ "$jobs" -gt 0 ]; then
    arguments+=(-j "$jobs")
fi

set +e
if [ "$mode" = "review" ]; then
    review_log=$(mktemp)
    "$runner" "${arguments[@]}" "$file_regex" >"$review_log" 2>&1
else
    "$runner" "${arguments[@]}" "$file_regex"
fi
status=$?
set -e

if [ "$mode" = "review" ]; then
    if [ ! -s "$fixes_file" ] && [ "$status" -ne 0 ]; then
        cat "$review_log" >&2
    fi
    rm -f -- "$review_log"
fi
if [ "$status" -eq 0 ]; then
    exit 0
elif [ "$status" -eq 1 ]; then
    # Reserve an outer status so Docker/dispatcher failures are not mistaken for
    # clang-tidy diagnostics.
    exit 42
else
    exit "$status"
fi
INSIDE_DOCKER
dispatch_status=$?
set -e

if [ "$dispatch_status" -eq 42 ]; then
    clang_tidy_status=1
elif [ "$dispatch_status" -eq 0 ]; then
    clang_tidy_status=0
else
    fail "Docker or clang-tidy tooling failed (exit code $dispatch_status)"
fi

case "$MODE" in
    dry-run)
        if [ "$clang_tidy_status" -eq 0 ]; then
            echo "Dry run completed: no diagnostics in '$SCOPE_DESCRIPTION'. No files changed."
        else
            echo "Dry run completed: clang-tidy reported diagnostics above. No files changed." >&2
        fi
        exit "$clang_tidy_status"
        ;;
    apply-all)
        if [ "$clang_tidy_status" -eq 0 ]; then
            echo "clang-tidy apply phase completed without diagnostics."
        else
            echo "clang-tidy apply phase reported diagnostics and applied available fixes."
        fi

        echo "Rechecking the updated files with the same analysis and fix scope..."
        verify_scope="--library-only"
        if [ "$SCOPE_MODE" = "dependencies" ]; then
            verify_scope="--include-dependencies"
        fi
        set +e
        "$REPO_ROOT/fix_clang_tidy.sh" --dry-run "$verify_scope" \
            --build-dir "$BUILD_DIR" --jobs "$JOBS" "$LIBRARY_DIR"
        verify_status=$?
        set -e
        if [ "$verify_status" -eq 0 ]; then
            echo "All diagnostics in '$SCOPE_DESCRIPTION' are resolved."
        else
            echo "Some diagnostics remain after applying all available fixes." >&2
            echo "Run the interactive reviewer to handle diagnostics without an automatic fix." >&2
        fi
        exit "$verify_status"
        ;;
    review)
        if [ ! -s "$FIXES_FILE" ]; then
            if [ "$clang_tidy_status" -eq 0 ]; then
                echo "clang-tidy found no diagnostics in '$SCOPE_DESCRIPTION'. No files changed."
                exit 0
            fi
            fail "clang-tidy failed without exporting reviewable diagnostics"
        fi
        reviewer_args=(
            --ui "$REVIEW_UI"
            --port "$WEB_PORT"
            --library-root "$REVIEW_ROOT"
            --display-root "$REPO_ROOT"
        )
        if [ "$REVIEW_OPEN" = false ]; then
            reviewer_args+=(--no-open)
        fi
        "$PYTHON" "$REVIEWER" "${reviewer_args[@]}" "$FIXES_FILE"
        ;;
esac
