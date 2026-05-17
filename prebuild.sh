#!/bin/sh
# Stage lib.asn + asn_refresh.py from the mapping main repo into this
# container's build context. Must be run before `docker build`.
#
# Usage:
#     ./prebuild.sh             # from inside src/tools/asn_offline/
#     # or, from anywhere:
#     /path/to/src/tools/asn_offline/prebuild.sh
#
# The meta builder calls this automatically once Phase 2b wires staging
# into meta.py. Until then, run it manually.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"

LIB_ASN_SRC="$REPO_ROOT/src/lib/asn"
REFRESH_SRC="$REPO_ROOT/src/workflow/jobs/asn_refresh.py"

if [ ! -d "$LIB_ASN_SRC" ]; then
    echo "[prebuild] missing $LIB_ASN_SRC — are you running this from a checkout of mapping?" >&2
    exit 1
fi
if [ ! -f "$REFRESH_SRC" ]; then
    echo "[prebuild] missing $REFRESH_SRC" >&2
    exit 1
fi

# Vendor lib.asn under the name lib_asn so it doesn't collide with the
# host repo's `lib` namespace when the container is built from this dir.
rm -rf "$HERE/lib_asn"
cp -r "$LIB_ASN_SRC" "$HERE/lib_asn"
# Drop __pycache__ — gets rebuilt inside the image.
find "$HERE/lib_asn" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Refresh job — the container imports it as a top-level module from /app.
cp "$REFRESH_SRC" "$HERE/refresh.py"

echo "[prebuild] staged:"
echo "  $LIB_ASN_SRC -> $HERE/lib_asn/"
echo "  $REFRESH_SRC -> $HERE/refresh.py"
echo "[prebuild] now run:  docker build -t meta-asn-offline:latest $HERE"
