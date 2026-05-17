# asn_offline — thin proxy producing asnmap-shaped JSONL.
#
# Fans each IP lookup out to:
#   1. iptoasn-webservice (jedisct1/iptoasn-webservice, separate container)
#      for ASN number, name, and country
#   2. local pyasn loaded from ipasn.dat (populated by asn-refresh job)
#      for the full announced prefix list of that ASN
#
# Build context: this directory (src/tools/asn_offline).
#     docker compose build asn-offline
FROM python:3.12.4-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && apt-get purge -y --auto-remove gcc libc6-dev

COPY app.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

# Dataset (ipasn.dat only) is populated by the host-cron-driven asn-refresh
# job and mounted read-only here. iptoasn-webservice fetches its own data
# independently.
ENV ASN_DATA_DIR=/var/lib/asn_data
VOLUME ["/var/lib/asn_data"]

ENV PORT=8000
EXPOSE 8000

CMD ["/app/entrypoint.sh"]
