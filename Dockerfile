# asn_offline — Python-library tool exposed over HTTP for IaC fleet.
#
# Bundles `lib.asn` (offline ASN/IP/Org lookup) and the daily refresh job
# (workflow.jobs.asn_refresh) into a single container. The lookup endpoint
# reads through $ASN_DATA_DIR, a symlink under the shared $ASN_VOLUME_ROOT.
#
# Build context: this directory (src/tools/asn_offline). The build expects
# `lib_asn/` and `refresh.py` to be staged into the build context before
# docker build (run prebuild.sh until the meta builder owns that step).
#
#     docker build -t meta-asn-offline:latest src/tools/asn_offline
FROM python:3.12.4-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r /app/requirements.txt

# lib.asn and the refresh job are copied in by the meta builder before build.
# The COPY paths below assume the build-context staging layout.
COPY . /app

# Dataset volume — populated by the refresh sidecar. First boot pulls a fresh
# snapshot synchronously before the FastAPI service starts serving traffic.
ENV ASN_VOLUME_ROOT=/var/lib/asn_offline
VOLUME ["/var/lib/asn_offline"]

ENV PORT=8000
EXPOSE 8000

# Fail the build instead of producing an image that imports only at runtime.
RUN test -f /app/refresh.py \
    && test -f /app/lib_asn/lookup.py \
    && chmod +x /app/entrypoint.sh

# entrypoint.sh: ensure dataset, start refresh sidecar in background, then
# hand off to uvicorn in foreground.
CMD ["/app/entrypoint.sh"]
