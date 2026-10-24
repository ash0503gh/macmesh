#!/usr/bin/env bash
# Zips build/macmesh.app for a GitHub release, with its SHA-256 beside it (install.sh checks it).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d build/macmesh.app ] || scripts/build-app.sh
zip=build/macmesh-macos-arm64.zip
rm -f "$zip" "$zip.sha256"
ditto -c -k --keepParent build/macmesh.app "$zip"
(cd build && shasum -a 256 "$(basename "$zip")" > "$(basename "$zip").sha256")
ls -la "$zip" "$zip.sha256"
