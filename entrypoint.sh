#!/bin/sh
# asn_offline container entrypoint.
#
# Volume layout:
#   $ASN_VOLUME_ROOT/
#     dataset.<timestamp>/                ← versioned snapshot built by refresh
#     dataset.current  -> dataset.<timestamp>/   ← atomically swapped
#
# $ASN_DATA_DIR points at dataset.current. lib_asn.loader reads through the
# symlink so the lookup process picks up new datasets without restarting.
#
# Boot order:
#   1. Bootstrap dataset.current if missing (first boot, synchronous).
#   2. Start refresh_sidecar.py (apscheduler) in the background.
#   3. Hand off PID 1 to uvicorn (FastAPI lookup service).
set -eu

: "${ASN_VOLUME_ROOT:=/var/lib/asn_offline}"
if [ -z "${ASN_DATA_DIR:-}" ] || [ "$ASN_DATA_DIR" = "$ASN_VOLUME_ROOT" ]; then
    ASN_DATA_DIR="$ASN_VOLUME_ROOT/dataset.current"
fi
: "${ASN_REFRESH_CRON:=0 3 * * *}"
: "${PORT:=8000}"
export ASN_VOLUME_ROOT ASN_DATA_DIR ASN_REFRESH_CRON

mkdir -p "$ASN_VOLUME_ROOT"

# Bootstrap: if dataset.current is missing or broken, pull a fresh snapshot
# synchronously so the FastAPI service can serve immediately after launch.
if [ ! -e "$ASN_DATA_DIR/MANIFEST.json" ]; then
    STAMP=$(date -u +%Y%m%dT%H%M%SZ)
    BOOTSTRAP_DIR=$(mktemp -d "$ASN_VOLUME_ROOT/dataset.$STAMP.XXXXXX")
    echo "[asn_offline] bootstrapping dataset into $BOOTSTRAP_DIR (first boot)" >&2
    if python /app/refresh.py --data-dir "$BOOTSTRAP_DIR"; then
        ln -sfn "$BOOTSTRAP_DIR" "$ASN_VOLUME_ROOT/dataset.current.tmp"
        mv -Tf "$ASN_VOLUME_ROOT/dataset.current.tmp" "$ASN_VOLUME_ROOT/dataset.current"
        echo "[asn_offline] bootstrap complete, dataset.current -> $BOOTSTRAP_DIR" >&2
    else
        echo "[asn_offline] bootstrap refresh failed; service will start but lookups will return 503 until next refresh" >&2
    fi
fi

# Daily refresh sidecar — runs apscheduler, atomic symlink swap on success.
python /app/refresh_sidecar.py &
SIDECAR_PID=$!
echo "[asn_offline] refresh sidecar pid=$SIDECAR_PID, cron='$ASN_REFRESH_CRON'" >&2

# Foreground: FastAPI lookup service.
exec uvicorn app:app --host 0.0.0.0 --port "$PORT"
