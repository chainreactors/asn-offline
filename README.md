# asn_offline

IaC container that serves offline ASN / IP / Organization lookups.
Replaces the in-process `lib.asn.ASNmapOffline` tool from the mapping main
repo per [capability-boundary.md](../../../docs/guides/capability-boundary.md).

## Status

**Phase 2a (skeleton)** — code-only. Not yet wired into capability routing.
The mapping main repo still serves `map_asn` via in-process `ASNmapOffline`.
Phase 2b (deploy + cutover) requires a real environment with Docker + IaC fleet.

## Architecture

```
┌─────────────────────────────────────────────┐
│  asn_offline container                      │
│                                             │
│  ┌─────────────────┐   ┌──────────────────┐ │
│  │ FastAPI app     │   │ refresh sidecar  │ │
│  │ (uvicorn fg)    │   │ (apscheduler bg) │ │
│  │                 │   │                  │ │
│  │ POST /tools/    │   │ cron 03:00 UTC   │ │
│  │   asn_offline/  │   │  ↓               │ │
│  │   invoke        │   │ refresh_all()    │ │
│  │      ↓          │   │  ↓               │ │
│  │  lib_asn.lookup │   │ atomic symlink   │ │
│  └────────┬────────┘   └────────┬─────────┘ │
│           │                     │           │
│           └─────────┬───────────┘           │
│                     ↓                       │
│        /var/lib/asn_offline/                │
│        ├─ dataset.20260514T030000Z.ab12cd/  │
│        │  ├─ ipasn.dat                      │
│        │  ├─ ip2asn-combined.tsv            │
│        │  ├─ asn_meta.sqlite                │
│        │  └─ MANIFEST.json                  │
│        └─ dataset.current -> dataset.…      │
└─────────────────────────────────────────────┘
              ↑                ↑
       external volume    IaC fleet
```

## Build context staging

The Dockerfile expects two staged paths in this directory before `docker
build`; the build fails fast if either is missing:

| Source in mapping repo | Target in build context |
|---|---|
| `src/lib/asn/` | `lib_asn/` |
| `src/workflow/jobs/asn_refresh.py` | `refresh.py` |

Until Phase 2b wires this into the meta builder, stage them manually:

```sh
src/tools/asn_offline/prebuild.sh
docker build -t meta-asn-offline:latest src/tools/asn_offline
```

`asn_offline` is registered in `src/tools/serverless/meta/meta.yaml`, but it
is marked `default_build: false` while this staging step is manual. A default
`mim build` skips it; build it explicitly after staging with
`mim build -t asn_offline` or include it via `-t all`.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `ASN_VOLUME_ROOT` | `/var/lib/asn_offline` | Dataset volume root |
| `ASN_DATA_DIR` | `$ASN_VOLUME_ROOT/dataset.current` | Active dataset symlink read by lookup |
| `ASN_REFRESH_CRON` | `0 3 * * *` | UTC cron for sidecar refresh |
| `ASN_REFRESH_MRT_URL` | `https://data.ris.ripe.net/rrc00/latest-bview.gz` | RIPE-RIS BGP MRT source |
| `ASN_REFRESH_IPTOASN_URL` | `https://iptoasn.com/data/ip2asn-combined.tsv.gz` | IPtoASN source |
| `ASN_REFRESH_PEERINGDB_URL` | `https://www.peeringdb.com/api/net` | PeeringDB source |
| `ASN_REFRESH_CAIDA_URL` | `https://publicdata.caida.org/datasets/as-organizations/latest.as-org2info.txt.gz` | CAIDA AS-org source |
| `ASN_REFRESH_RIR_{ARIN,RIPE,APNIC,AFRINIC,LACNIC}` | upstream RIR FTP | Per-RIR delegated-stats |
| `ASN_REFRESH_HTTP_TIMEOUT` | `600` | Per-source download timeout (s) |
| `PORT` | `8000` | FastAPI listen port |
| `LOG_LEVEL` | `INFO` | Python logging level |

For air-gapped sites, point every `ASN_REFRESH_*_URL` env at an internal
mirror — the refresh code already supports overrides.

## Endpoints

- `GET /health` — liveness + dataset readiness; returns HTTP 503 until the
  dataset is present and loadable
- `POST /tools/asn_offline/invoke` — asnmap-compatible lookup

Request body:

```json
{
  "cmd": ["-d", "example.com", "-j"],
  "stdin": "OC44LjguOAoxLjEuMS4xCg==",
  "invoke_id": "optional-tracking-id"
}
```

`stdin` is base64 in normal matrix calls. For direct local curl smoke tests,
send plain text only with `"stdin_encoding": "plain"` so the service does not
guess between raw text and base64.

Response (`stdout` / `stderr` are base64 strings, matching matrix
`ToolResponse`):

```json
{
  "invoke_id": "optional-tracking-id",
  "tool": "asnmap_offline",
  "cmd": ["-d", "example.com", "-j"],
  "success": true,
  "returncode": 0,
  "stdout": "eyJpbnB1dCI6ICJleGFtcGxlLmNvbSIsICJhc19udW1iZXIiOiAiQVMxNTE2OSJ9",
  "stderr": "",
  "jsonlines": [{"input": "...", "as_number": "AS15169", "as_name": "...", "as_country": "..."}],
  "error_message": null
}
```

The `cmd`, `stdin`, and response contract mirrors matrix `ToolRequest` /
`ToolResponse`, while argv parsing mirrors
`lib.asn.tool.ASNmapOffline._parse_cmd`.

## Data sources

The dataset is built from five public sources merged into a single SQLite
index. See `refresh.py` (a copy of `workflow.jobs.asn_refresh`) for the full
join logic. Sources:

- **RIPE-RIS rrc00 latest-bview** — IP → ASN radix (via pyasn)
- **IPtoASN combined** — IP range → ASN + country (in-memory bisect)
- **PeeringDB** — ASN → org name
- **CAIDA AS-organizations** — fallback for ASNs missing from PeeringDB
- **RIR delegated-stats** (ARIN/RIPE/APNIC/AFRINIC/LACNIC) — country backfill

## Lifecycle

1. First boot: `entrypoint.sh` sees no `MANIFEST.json`, runs a blocking
   `python /app/refresh.py` to bootstrap a usable dataset before serving.
2. Steady state: refresh sidecar fires daily at 03:00 UTC (configurable);
   on success, atomically swaps `dataset.current` symlink to a fresh
   `dataset.<timestamp>/` directory.
3. The FastAPI lookup process detects the changed symlink / manifest and
   reloads the in-memory dataset on the next request — no restart needed.
