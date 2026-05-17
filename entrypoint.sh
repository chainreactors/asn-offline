#!/bin/sh
# asn_offline container entrypoint.
#
# ipasn.dat lives in $ASN_DATA_DIR (shared volume written by the
# host-cron-driven asn-refresh oneshot job). Without it, prefix lookups
# return empty as_range; the iptoasn-webservice fan-out still works.
set -eu

: "${ASN_DATA_DIR:=/var/lib/asn_data}"
: "${PORT:=8000}"
export ASN_DATA_DIR

if [ ! -e "$ASN_DATA_DIR/ipasn.dat" ]; then
    echo "[asn_offline] $ASN_DATA_DIR/ipasn.dat missing — as_range will be empty until asn-refresh populates the volume" >&2
fi

exec uvicorn app:app --host 0.0.0.0 --port "$PORT"
