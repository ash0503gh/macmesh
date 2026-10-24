#!/bin/sh
# Installs macmesh.app (with its own engine) from the latest GitHub release:
#   curl -fsSL https://raw.githubusercontent.com/ash0503gh/macmesh/main/install.sh | sh
# It checks the download against the release's SHA-256 file, puts the app in /Applications (or
# ~/Applications if that isn't writable), and opens it.
set -eu

repo="ash0503gh/macmesh"
asset="macmesh-macos-arm64.zip"
base="https://github.com/$repo/releases/latest/download"

fail() { echo "macmesh: $*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || fail "macmesh runs on macOS only."
[ "$(uname -m)" = arm64 ] || fail "this build is for Apple silicon Macs (M1 or later)."
major="$(sw_vers -productVersion | cut -d. -f1)"
[ "$major" -ge 14 ] || fail "macmesh needs macOS 14 or later (this Mac has $(sw_vers -productVersion))."

if pgrep -xq Macmesh; then
    fail "macmesh is running. Quit it (menu bar icon > Quit), then run this again."
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading macmesh…"
curl -fsSL -o "$tmp/$asset" "$base/$asset" || fail "couldn't download $base/$asset"
curl -fsSL -o "$tmp/$asset.sha256" "$base/$asset.sha256" || fail "couldn't download the checksum"
expected="$(awk '{print $1}' "$tmp/$asset.sha256")"
actual="$(shasum -a 256 "$tmp/$asset" | awk '{print $1}')"
[ -n "$expected" ] && [ "$expected" = "$actual" ] || fail "the download doesn't match its checksum; not installing."

ditto -x -k "$tmp/$asset" "$tmp/unpacked"
[ -d "$tmp/unpacked/macmesh.app" ] || fail "the download doesn't contain macmesh.app."

dest="/Applications"
[ -w "$dest" ] || { dest="$HOME/Applications"; mkdir -p "$dest"; }
rm -rf "$dest/macmesh.app"
ditto "$tmp/unpacked/macmesh.app" "$dest/macmesh.app"
xattr -dr com.apple.quarantine "$dest/macmesh.app" 2>/dev/null || true

echo "Installed $dest/macmesh.app"
echo "For the command line and agents: $dest/macmesh.app/Contents/Resources/engine/bin/macmesh"
open "$dest/macmesh.app"
