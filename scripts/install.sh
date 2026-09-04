#!/bin/sh
# Release-bound SCS installer for macOS arm64 and Linux x86_64.

set -eu

readonly SCS_REPOSITORY="leonardoventurini/scs"
readonly SCS_INSTALLER_VERSION="@SCS_VERSION@"
readonly UV_VERSION="0.12.9"

requested_version="$SCS_INSTALLER_VERSION"
check_only=false

usage() {
    printf '%s\n' "usage: install.sh [--version VERSION] [--check]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            requested_version="$2"
            shift 2
            ;;
        --check)
            check_only=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$requested_version" in
    @SCS_VERSION@)
        printf '%s\n' "This source installer requires --version; release assets embed an exact version." >&2
        exit 2
        ;;
    ''|*[!0-9A-Za-z.-]*)
        printf '%s\n' "Invalid SCS version: $requested_version" >&2
        exit 2
        ;;
esac

command -v curl >/dev/null 2>&1 || { printf '%s\n' "curl is required" >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { printf '%s\n' "tar is required" >&2; exit 1; }

os_name=$(uname -s)
architecture=$(uname -m)
case "$os_name:$architecture" in
    Darwin:arm64)
        wheel="scs-${requested_version}-cp314-cp314-macosx_11_0_arm64.whl"
        uv_target="aarch64-apple-darwin"
        ;;
    Linux:x86_64)
        wheel="scs-${requested_version}-cp314-cp314-manylinux_2_28_x86_64.whl"
        uv_target="x86_64-unknown-linux-gnu"
        ;;
    *)
        printf 'Unsupported platform: %s %s\n' "$os_name" "$architecture" >&2
        exit 1
        ;;
esac

if command -v shasum >/dev/null 2>&1; then
    checksum_command="shasum -a 256"
elif command -v sha256sum >/dev/null 2>&1; then
    checksum_command="sha256sum"
else
    printf '%s\n' "shasum or sha256sum is required" >&2
    exit 1
fi

if [ "$check_only" = true ]; then
    printf 'SCS %s prerequisites are available for %s %s.\n' "$requested_version" "$os_name" "$architecture"
    exit 0
fi

temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/scs-install.XXXXXX")
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM
chmod 700 "$temporary_directory"

release_base="https://github.com/${SCS_REPOSITORY}/releases/download/v${requested_version}"
constraints="scs-${requested_version}-constraints.txt"

download() {
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        --output "$temporary_directory/$1" "$2/$1"
}

download "SHA256SUMS" "$release_base"
download "$wheel" "$release_base"
download "$constraints" "$release_base"

verify_asset() {
    asset="$1"
    expected=$(awk -v name="$asset" '$2 == name || $2 == "*" name { print $1 }' "$temporary_directory/SHA256SUMS")
    [ -n "$expected" ] || { printf 'No checksum for %s\n' "$asset" >&2; exit 1; }
    actual=$(cd "$temporary_directory" && $checksum_command "$asset" | awk '{print $1}')
    [ "$actual" = "$expected" ] || { printf 'Checksum mismatch for %s\n' "$asset" >&2; exit 1; }
}

verify_asset "$wheel"
verify_asset "$constraints"

if command -v uv >/dev/null 2>&1; then
    uv_command=$(command -v uv)
else
    uv_archive="uv-${uv_target}.tar.gz"
    uv_base="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"
    download "$uv_archive" "$uv_base"
    download "${uv_archive}.sha256" "$uv_base"
    expected=$(awk '{print $1}' "$temporary_directory/${uv_archive}.sha256")
    actual=$(cd "$temporary_directory" && $checksum_command "$uv_archive" | awk '{print $1}')
    [ "$actual" = "$expected" ] || { printf '%s\n' "uv checksum mismatch" >&2; exit 1; }
    tar -xzf "$temporary_directory/$uv_archive" -C "$temporary_directory"
    uv_command="$temporary_directory/uv-${uv_target}/uv"
fi

if command -v scs >/dev/null 2>&1; then
    scs daemon stop >/dev/null 2>&1 || true
fi

"$uv_command" tool install \
    --python 3.14 \
    --constraints "$temporary_directory/$constraints" \
    --force \
    "$temporary_directory/$wheel"

launcher="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}/scs"
[ -x "$launcher" ] || { printf 'Installed SCS launcher not found at %s\n' "$launcher" >&2; exit 1; }
installed_version=$($launcher version)
[ "$installed_version" = "$requested_version" ] || {
    printf 'Installed version mismatch: expected %s, found %s\n' "$requested_version" "$installed_version" >&2
    exit 1
}

printf 'Installed SCS %s at %s\n' "$installed_version" "$launcher"
printf 'Configure your MCP harness command as: %s mcp\n' "$launcher"
printf '%s\n' "The shared daemon starts lazily and stops after the final MCP bridge exits."
printf '%s\n' "Persistent data remains under SCS_HOME (default: $HOME/.scs)."
