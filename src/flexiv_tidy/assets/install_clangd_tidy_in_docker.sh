#!/usr/bin/env bash

# Install the pinned clangd-tidy runtime inside the Ubuntu build container.
# This script is idempotent and supports both Bitbucket's root user and the
# passwordless-sudo flexiv user created by docker/shell_entry.sh.

set -euo pipefail

readonly CLANGD_MAJOR_VERSION="${CLANGD_MAJOR_VERSION:-15}"
readonly CLANGD_TIDY_VERSION="${CLANGD_TIDY_VERSION:-1.1.1}"
readonly CLANGD_EXECUTABLE="clangd-${CLANGD_MAJOR_VERSION}"

run_as_root() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "Error: run this installer as root or from ./shell-docker.sh." >&2
        exit 1
    fi
}

installed_clangd_tidy_version() {
    python3 - <<'PY' 2>/dev/null || true
import importlib.metadata

print(importlib.metadata.version("clangd-tidy"))
PY
}

clangd_ready=false
if command -v "$CLANGD_EXECUTABLE" >/dev/null 2>&1; then
    detected_major="$("$CLANGD_EXECUTABLE" --version | sed -n 's/.*version \([0-9][0-9]*\).*/\1/p' | head -1)"
    [[ "$detected_major" == "$CLANGD_MAJOR_VERSION" ]] && clangd_ready=true
fi

clangd_tidy_ready=false
if command -v clangd-tidy >/dev/null 2>&1 \
    && [[ "$(installed_clangd_tidy_version)" == "$CLANGD_TIDY_VERSION" ]]; then
    clangd_tidy_ready=true
fi

if [[ "$clangd_ready" == true && "$clangd_tidy_ready" == true ]]; then
    echo "clangd-tidy runtime already installed:"
    echo "  $("$CLANGD_EXECUTABLE" --version | head -1)"
    echo "  clangd-tidy $CLANGD_TIDY_VERSION"
    exit 0
fi

packages=(python3 python3-pip)
if [[ "$clangd_ready" != true ]]; then
    packages+=("clangd-${CLANGD_MAJOR_VERSION}")
fi

echo "Installing clangd-tidy runtime inside Docker..."
run_as_root apt-get update -qq
run_as_root env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y -qq --no-install-recommends "${packages[@]}"

if [[ "$clangd_tidy_ready" != true ]]; then
    run_as_root python3 -m pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        "clangd-tidy==$CLANGD_TIDY_VERSION"
fi

command -v "$CLANGD_EXECUTABLE" >/dev/null 2>&1 || {
    echo "Error: $CLANGD_EXECUTABLE was not installed." >&2
    exit 1
}
command -v clangd-tidy >/dev/null 2>&1 || {
    echo "Error: clangd-tidy was not installed." >&2
    exit 1
}
[[ "$(installed_clangd_tidy_version)" == "$CLANGD_TIDY_VERSION" ]] || {
    echo "Error: expected clangd-tidy $CLANGD_TIDY_VERSION." >&2
    exit 1
}

echo "Installed clangd-tidy runtime:"
echo "  $("$CLANGD_EXECUTABLE" --version | head -1)"
echo "  clangd-tidy $CLANGD_TIDY_VERSION"
