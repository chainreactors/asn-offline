# asn_offline

Sidecar that serves offline ASN lookups to teamserver / worker. Replaces
the in-process `lib.asn.ASNmapOffline` tool from the mapping main repo.

## Architecture

Two cooperating containers:

```
                      ┌─────────────────────────────────────┐
                      │  host crontab                       │
                      │   └─ make asn-refresh (oneshot)     │
                      │      → builds ipasn.dat             │
                      └────────────────┬────────────────────┘
                                       │ writes
                                       ▼
                          asn_data volume (shared)
                          └─ ipasn.dat   (pyasn radix, MRT)
                                       │
                                       │ read-only
                                       ▼
┌────────────────────────┐    ┌─────────────────────────────┐
│ iptoasn-webservice     │◄───│ asn-offline (proxy)         │
│ (jedisct1, internal)   │    │  FastAPI :8000              │
│  :53661                │    │  /tools/asn_offline/invoke  │
│  IP→{ASN, name, ctry}  │    │  ───────────────────────────│
│  refreshes own data    │    │  per IP: fan out to iptoasn │
│  every 60 min          │    │  + pyasn.get_as_prefixes()  │
└────────────────────────┘    │  → asnmap-shaped JSONL      │
                              └──────────────┬──────────────┘
                                             │ matrix ToolResponse
                                             ▼
                                    teamserver / worker
                                  (via RemoteAsnOfflineTool)
```

`iptoasn-webservice` is jedisct1's open-source Rust service
(<https://github.com/jedisct1/iptoasn-webservice>). It pre-downloads
[iptoasn.com](https://iptoasn.com/) data and serves all lookups from
memory — no per-query rate limit, no external API call.

`pyasn` is loaded from `ipasn.dat` (RouteViews MRT, built daily by
`workflow.jobs.asn_refresh`). Used only for `get_as_prefixes(asn)` to
fill `as_range` — the CSTX parser yields one CIDR node per prefix, so
this fan-out is what produces the full per-ASN CIDR topology in the
graph.

## CLI surface

Only the IP path of ProjectDiscovery asnmap is supported (no production
caller emits domain / ASN / org queries — see `ip_enrichment_flow`
and `asnmap_baseline`):

- `-i <ip>` — single input
- bare positional tokens — input
- newline-separated stdin — inputs

`-d` / `-a` / `-org` flags are silently dropped.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `ASN_DATA_DIR` | `/var/lib/asn_data` | Dataset dir (read-only mount) |
| `IPTOASN_URL` | `http://iptoasn:53661` | Internal upstream |
| `IPTOASN_TIMEOUT` | `5.0` | Per-request seconds |
| `PORT` | `8000` | FastAPI listen port |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Endpoints

- `GET /health` — liveness + readiness; returns 503 when `ipasn.dat` is
  missing or `iptoasn-webservice` is unreachable
- `POST /tools/asn_offline/invoke` — matrix `ToolRequest` →
  `ToolResponse` with asnmap JSONL in `stdout` (base64)

## Response schema

`stdout` is newline-delimited JSON, one record per input:

```json
{
  "input": "8.8.8.8",
  "as_number": "AS15169",
  "as_name": "GOOGLE",
  "as_country": "US",
  "as_range": ["8.8.8.0/24", "8.8.4.0/24", "..."]
}
```

Field names match what `cstx/plugins/easm/asnmap.py:ASNmapItem` expects.

## Dataset lifecycle

- `iptoasn-webservice` self-refreshes from iptoasn.com every 60 minutes.
- `ipasn.dat` is built by the host-cron oneshot:
  `make asn-refresh` → `docker compose --profile jobs run --rm asn-refresh`.
  Lookups soft-fail (empty `as_range`) until the first refresh succeeds;
  the iptoasn-webservice fan-out continues to work.
