#!/usr/bin/env bash
# Builds build/macmesh.app: the Swift app, its own Python, and the macmesh engine, signed ad hoc.
# The Python is python-build-standalone, pinned by release and SHA-256. PYTHON_TARBALL points at a
# copy you already have; otherwise it's fetched into build/cache. zeroconf and ifaddr (the only
# packages the engine needs) are copied from ZEROCONF_PYTHON's site-packages (default python3),
# which must be a CPython 3.13 on Apple silicon like the bundled one.
set -euo pipefail
cd "$(dirname "$0")/.."
export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"

PY_RELEASE=20261009
PY_FILE="cpython-3.13.16+${PY_RELEASE}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_SHA256=11785155d0f70e634178219c20925d8f20be00a3da4684fc4f5019dd15602ef7
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/${PY_FILE//+/%2B}"

mkdir -p build/cache
tarball="${PYTHON_TARBALL:-build/cache/$PY_FILE}"
[ -f "$tarball" ] || curl -fsSL -o "$tarball" "$PY_URL"
echo "$PY_SHA256  $tarball" | shasum -a 256 -c - >/dev/null || { echo "Python archive checksum mismatch: $tarball" >&2; exit 1; }

swift build -c release
bin="$(swift build -c release --show-bin-path)"
app=build/macmesh.app
engine="$app/Contents/Resources/engine"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$engine/lib"
cp "$bin/Macmesh" "$app/Contents/MacOS/"
cp Support/Info.plist "$app/Contents/"
tar -xzf "$tarball" -C "$engine"   # unpacks to engine/python
rsync -a --exclude __pycache__ ../macmesh "$engine/lib/"

site_python="${ZEROCONF_PYTHON:-python3}"
for pkg in zeroconf ifaddr; do
    src="$("$site_python" -c "import os, $pkg; print(os.path.dirname($pkg.__file__))")"
    rsync -a --exclude __pycache__ "$src" "$engine/lib/"
done
# A `macmesh` command inside the app, for the shell and for agents (`macmesh mcp`).
mkdir -p "$engine/bin"
cat > "$engine/bin/macmesh" <<'SH'
#!/bin/sh
engine="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONPATH="$engine/lib" PYTHONNOUSERSITE=1 exec "$engine/python/bin/python3" -m macmesh.cli "$@"
SH
chmod +x "$engine/bin/macmesh"
"$engine/python/bin/python3" -I -c "import sys; sys.path.insert(0, '$engine/lib'); import macmesh.cli, zeroconf" \
    || { echo "the bundled engine doesn't import" >&2; exit 1; }

codesign --force --deep --sign - "$app"
du -sh "$app" | awk '{print "built " $2 " (" $1 ")"}'
