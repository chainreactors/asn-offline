# jedisct1/iptoasn-webservice — built from upstream source.
#
# Upstream: https://github.com/jedisct1/iptoasn-webservice
#
# This image bundles only the Rust web service binary. Data is downloaded
# at container startup from $IPTOASN_DBURL (default iptoasn.com combined
# TSV) and refreshed every $IPTOASN_REFRESH minutes. There is no
# per-query rate limit — all lookups are served from the in-memory radix.
FROM rust:1.83-slim AS build

WORKDIR /src
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*

ARG IPTOASN_REF=master
RUN git clone --depth 1 --branch "${IPTOASN_REF}" https://github.com/jedisct1/iptoasn-webservice.git . \
    && cargo build --release \
    && strip target/release/iptoasn-webservice

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin app

COPY --from=build /src/target/release/iptoasn-webservice /usr/local/bin/iptoasn-webservice

USER app
WORKDIR /home/app

ENV IPTOASN_LISTEN=0.0.0.0:53661
ENV IPTOASN_DBURL=https://iptoasn.com/data/ip2asn-combined.tsv.gz
ENV IPTOASN_REFRESH=60

EXPOSE 53661

CMD ["/bin/sh", "-c", "exec /usr/local/bin/iptoasn-webservice --listen ${IPTOASN_LISTEN} --dburl ${IPTOASN_DBURL} --refresh ${IPTOASN_REFRESH}"]
