"""Daily refresh job for the offline ASN dataset.

Pulls five public sources and bakes them into the layout consumed by
`lib.asn`:

    <data_dir>/ipasn.dat               # Routeviews/RIPE-RIS MRT → pyasn binary
    <data_dir>/ip2asn-combined.tsv     # IPtoASN raw (used by lib.asn radix)
    <data_dir>/peeringdb_net.json      # PeeringDB raw (org name source)
    <data_dir>/caida_as_org.txt        # CAIDA AS-org (fills PeeringDB gaps)
    <data_dir>/rir/delegated-*.txt     # RIR delegated-stats (country backfill)
    <data_dir>/asn_meta.sqlite         # baked asn_meta + org_fts FTS5 table
    <data_dir>/MANIFEST.json           # source freshness + checksums

Each download writes a `.tmp` sibling and atomically renames into place so a
crash mid-refresh leaves the previous dataset intact. Individual source
failures are logged but do not abort the job — a partial refresh is better
than no refresh.

CLI: `python -m workflow.jobs.asn_refresh [--force] [--source <name> ...]`
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

import requests

logger = logging.getLogger(__name__)


# ── Source URLs (override via env for air-gapped mirrors) ──────────────


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


# RIPE RIS rrc00 publishes a stable "latest-bview" symlink — avoids scraping
# Routeviews' HTML index. Air-gapped sites can repoint at a local mirror.
MRT_URL = _env(
    "ASN_REFRESH_MRT_URL",
    "https://data.ris.ripe.net/rrc00/latest-bview.gz",
)
IPTOASN_URL = _env(
    "ASN_REFRESH_IPTOASN_URL",
    "https://iptoasn.com/data/ip2asn-combined.tsv.gz",
)
PEERINGDB_URL = _env(
    "ASN_REFRESH_PEERINGDB_URL",
    "https://www.peeringdb.com/api/net",
)
CAIDA_URL = _env(
    "ASN_REFRESH_CAIDA_URL",
    # CAIDA publishes quarterly; this points at the "latest" symlink in their
    # dataset directory. Sites with strict outbound rules should mirror.
    "https://publicdata.caida.org/datasets/as-organizations/latest.as-org2info.txt.gz",
)
RIR_URLS = {
    "arin": _env("ASN_REFRESH_RIR_ARIN", "https://ftp.arin.net/pub/stats/arin/delegated-arin-extended-latest"),
    "ripe": _env("ASN_REFRESH_RIR_RIPE", "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-extended-latest"),
    "apnic": _env("ASN_REFRESH_RIR_APNIC", "https://ftp.apnic.net/stats/apnic/delegated-apnic-extended-latest"),
    "afrinic": _env("ASN_REFRESH_RIR_AFRINIC", "https://ftp.afrinic.net/pub/stats/afrinic/delegated-afrinic-extended-latest"),
    "lacnic": _env("ASN_REFRESH_RIR_LACNIC", "https://ftp.lacnic.net/pub/stats/lacnic/delegated-lacnic-extended-latest"),
}

HTTP_TIMEOUT_SECONDS = int(_env("ASN_REFRESH_HTTP_TIMEOUT", "600"))
CHUNK_SIZE = 1 << 16


# ── Manifest model (light dict for write; reader is in lib.asn) ────────


@dataclass
class _SourceResult:
    path: Path
    sha256: str
    generated_at: datetime
    record_count: int = 0


@dataclass
class _RefreshState:
    data_dir: Path
    sources: dict[str, _SourceResult] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


# ── Helpers ────────────────────────────────────────────────────────────


@contextmanager
def _atomic_write(target: Path, suffix: str = ".tmp") -> Iterator[Path]:
    """Yield a temp path adjacent to `target`; rename on success."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + suffix)
    try:
        yield tmp
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    else:
        os.replace(tmp, target)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def _http_download(url: str, dest: Path, *, decompress_gzip: bool = False) -> None:
    """Stream `url` to `dest` with atomic rename.

    When `decompress_gzip=True`, the gzip stream is inflated on the fly so
    downstream code can mmap / bisect the plain text.
    """
    logger.info("downloading %s → %s", url, dest)
    with _atomic_write(dest) as tmp:
        with requests.get(url, stream=True, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            resp.raise_for_status()
            if decompress_gzip:
                with gzip.GzipFile(fileobj=resp.raw) as gz, tmp.open("wb") as out:
                    shutil.copyfileobj(gz, out, length=CHUNK_SIZE)
            else:
                with tmp.open("wb") as out:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            out.write(chunk)


def _count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as fh:
        for _ in fh:
            count += 1
    return count


# ── Per-source downloaders ─────────────────────────────────────────────


def download_iptoasn(dest: Path) -> _SourceResult:
    _http_download(IPTOASN_URL, dest, decompress_gzip=True)
    return _SourceResult(
        path=dest,
        sha256=_sha256_of(dest),
        generated_at=datetime.now(timezone.utc),
        record_count=_count_lines(dest),
    )


def download_peeringdb(dest: Path) -> _SourceResult:
    _http_download(PEERINGDB_URL, dest, decompress_gzip=False)
    try:
        payload = json.loads(dest.read_text(encoding="utf-8"))
        record_count = len(payload.get("data", []))
    except Exception:
        record_count = 0
    return _SourceResult(
        path=dest,
        sha256=_sha256_of(dest),
        generated_at=datetime.now(timezone.utc),
        record_count=record_count,
    )


def download_caida_as_org(dest: Path) -> _SourceResult:
    _http_download(CAIDA_URL, dest, decompress_gzip=True)
    return _SourceResult(
        path=dest,
        sha256=_sha256_of(dest),
        generated_at=datetime.now(timezone.utc),
        record_count=_count_lines(dest),
    )


def download_rir_delegated(dest_dir: Path) -> dict[str, _SourceResult]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, _SourceResult] = {}
    for name, url in RIR_URLS.items():
        dest = dest_dir / f"delegated-{name}.txt"
        try:
            _http_download(url, dest, decompress_gzip=False)
        except Exception as exc:
            logger.warning("RIR %s download failed: %s", name, exc)
            continue
        out[name] = _SourceResult(
            path=dest,
            sha256=_sha256_of(dest),
            generated_at=datetime.now(timezone.utc),
            record_count=_count_lines(dest),
        )
    return out


def download_routeviews(dest: Path) -> _SourceResult:
    """Download the latest MRT RIB and convert to pyasn's `ipasn.dat`.

    Uses `pyasn.mrtx.parse_mrt_file` + `dump_prefixes_to_file`. We download
    the gz stream into a tmpfile, parse, and write `ipasn.dat` atomically.
    """
    try:
        from pyasn import mrtx  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pyasn is required to build ipasn.dat") from exc

    with tempfile.NamedTemporaryFile(suffix=".bgpdump.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        logger.info("downloading MRT %s → %s", MRT_URL, tmp_path)
        with requests.get(MRT_URL, stream=True, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            resp.raise_for_status()
            with tmp_path.open("wb") as out:
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        out.write(chunk)

        logger.info("parsing MRT %s", tmp_path)
        prefixes = mrtx.parse_mrt_file(str(tmp_path), print_progress=False, skip_record_on_error=True)
        with _atomic_write(dest) as out_path:
            mrtx.dump_prefixes_to_file(prefixes, str(out_path), str(tmp_path))
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

    return _SourceResult(
        path=dest,
        sha256=_sha256_of(dest),
        generated_at=datetime.now(timezone.utc),
        record_count=len(prefixes),
    )


# ── Index builder ──────────────────────────────────────────────────────


def _parse_peeringdb(path: Path) -> dict[int, dict]:
    """ASN → {name, org_id, country} from PeeringDB net dump."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("PeeringDB parse failed: %s", exc)
        return {}
    out: dict[int, dict] = {}
    for net in payload.get("data", []):
        asn = net.get("asn")
        if not isinstance(asn, int):
            continue
        out[asn] = {
            "name": net.get("name") or "",
            "org_id": str(net.get("org_id") or ""),
            "country": "",  # PeeringDB country lives on the org record, skip
        }
    return out


def _parse_caida_as_org(path: Path) -> dict[int, dict]:
    """ASN → {name, org_id, country} from CAIDA's as-org2info text dump.

    File has two sections separated by a header line beginning with `# format`.
    The second section maps ASN → org_id; we join with the org table from the
    first section to surface name + country.
    """
    if not path.exists():
        return {}
    orgs: dict[str, dict] = {}
    asn_to_org: dict[int, str] = {}
    section = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# format:"):
            # `# format:org_id|...` vs `# format:aut|changed|org_id|...`
            first_field = line.split(":", 1)[1].strip().split("|", 1)[0].lower()
            if first_field == "org_id":
                section = "org"
            elif first_field == "aut":
                section = "asn"
            else:
                section = None
            continue
        if line.startswith("#"):
            continue
        parts = line.split("|")
        if section == "org" and len(parts) >= 5:
            org_id, _changed, name, country, _source = parts[:5]
            orgs[org_id] = {"name": name, "country": country}
        elif section == "asn" and len(parts) >= 3:
            try:
                asn = int(parts[0])
            except ValueError:
                continue
            asn_to_org[asn] = parts[2]
    out: dict[int, dict] = {}
    for asn, org_id in asn_to_org.items():
        org = orgs.get(org_id)
        if not org:
            continue
        out[asn] = {
            "name": org.get("name") or "",
            "org_id": org_id,
            "country": org.get("country") or "",
        }
    return out


def _parse_iptoasn_meta(path: Path) -> dict[int, dict]:
    """ASN → {name, country} aggregated from IPtoASN combined TSV."""
    if not path.exists():
        return {}
    out: dict[int, dict] = {}
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            parts = raw.rstrip("\n").split("\t")
            if len(parts) < 5:
                continue
            try:
                asn = int(parts[2])
            except ValueError:
                continue
            if asn == 0:
                continue
            if asn not in out:
                out[asn] = {"name": parts[4], "country": parts[3], "org_id": ""}
    return out


def build_indexes(data_dir: Path) -> int:
    """Bake `asn_meta.sqlite` with merged metadata + FTS5 org search index.

    Source priority for `name`/`country`: PeeringDB > CAIDA > IPtoASN.

    Returns the number of ASN rows written.
    """
    peeringdb = _parse_peeringdb(data_dir / "peeringdb_net.json")
    caida = _parse_caida_as_org(data_dir / "caida_as_org.txt")
    iptoasn = _parse_iptoasn_meta(data_dir / "ip2asn-combined.tsv")

    merged: dict[int, dict] = {}
    sources: dict[int, list[str]] = {}

    def _merge(asn: int, record: dict, source: str) -> None:
        entry = merged.setdefault(asn, {"name": "", "country": "", "org_id": ""})
        for key in ("name", "country", "org_id"):
            if not entry[key] and record.get(key):
                entry[key] = record[key]
        sources.setdefault(asn, []).append(source)

    # priority order: peeringdb first wins where it fills a field.
    for asn, rec in peeringdb.items():
        _merge(asn, rec, "peeringdb")
    for asn, rec in caida.items():
        _merge(asn, rec, "caida")
    for asn, rec in iptoasn.items():
        _merge(asn, rec, "iptoasn")

    sqlite_path = data_dir / "asn_meta.sqlite"
    with _atomic_write(sqlite_path) as tmp:
        conn = sqlite3.connect(tmp)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE asn_meta (
                    asn INTEGER PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT '',
                    org_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE org_fts USING fts5(
                    asn UNINDEXED,
                    name,
                    org_id UNINDEXED,
                    tokenize = 'unicode61 remove_diacritics 2'
                )
                """
            )
            rows = [
                (
                    asn,
                    rec["name"],
                    rec["country"],
                    rec["org_id"],
                    "+".join(sources.get(asn, [])),
                )
                for asn, rec in merged.items()
            ]
            conn.executemany(
                "INSERT INTO asn_meta(asn, name, country, org_id, source) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            fts_rows = [
                (asn, rec["name"], rec["org_id"])
                for asn, rec in merged.items()
                if rec["name"]
            ]
            conn.executemany(
                "INSERT INTO org_fts(asn, name, org_id) VALUES (?, ?, ?)",
                fts_rows,
            )
            conn.commit()
        finally:
            conn.close()

    logger.info("asn_meta.sqlite written: rows=%d", len(merged))
    return len(merged)


# ── Orchestrator ───────────────────────────────────────────────────────


def _write_manifest(state: _RefreshState) -> None:
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            name: {
                "generated_at": result.generated_at.isoformat(),
                "sha256": result.sha256,
                "record_count": result.record_count,
            }
            for name, result in state.sources.items()
        },
        "failures": state.failures,
    }
    target = state.data_dir / "MANIFEST.json"
    with _atomic_write(target) as tmp:
        tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


_DOWNLOADERS: dict[str, Callable[[Path], object]] = {
    "iptoasn": lambda data_dir: download_iptoasn(data_dir / "ip2asn-combined.tsv"),
    "peeringdb": lambda data_dir: download_peeringdb(data_dir / "peeringdb_net.json"),
    "caida": lambda data_dir: download_caida_as_org(data_dir / "caida_as_org.txt"),
    "rir": lambda data_dir: download_rir_delegated(data_dir / "rir"),
    "routeviews": lambda data_dir: download_routeviews(data_dir / "ipasn.dat"),
}


def refresh_all(
    data_dir: Path | None = None,
    *,
    sources: Iterable[str] | None = None,
    force: bool = False,
) -> _RefreshState:
    """Run every (or selected) source downloader, then rebuild indexes.

    `force` is currently a no-op — every run is a full re-download. It is
    accepted for forward-compat with a future cache-based skip mode.
    """
    if data_dir is None:
        from settings import ASN_DATA_DIR

        data_dir = ASN_DATA_DIR
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    selected = list(sources) if sources else list(_DOWNLOADERS.keys())
    state = _RefreshState(data_dir=data_dir)

    for source in selected:
        fn = _DOWNLOADERS.get(source)
        if fn is None:
            logger.warning("unknown source %r — skipping", source)
            continue
        try:
            result = fn(data_dir)
        except Exception as exc:
            logger.exception("source %s failed", source)
            state.failures[source] = repr(exc)
            continue
        if isinstance(result, dict):
            for sub_name, sub_result in result.items():
                state.sources[f"rir/{sub_name}"] = sub_result
        else:
            state.sources[source] = result  # type: ignore[assignment]

    # Indexes rebuild from whatever sources succeeded — partial dataset is
    # still useful as long as ipasn.dat is fresh.
    try:
        build_indexes(data_dir)
    except Exception as exc:
        logger.exception("index build failed")
        state.failures["index_build"] = repr(exc)

    _write_manifest(state)
    logger.info(
        "refresh complete: sources=%d failures=%d",
        len(state.sources),
        len(state.failures),
    )
    return state


# ── CLI ────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workflow.jobs.asn_refresh")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--source", dest="sources", action="append", default=None,
                        help="restrict to one or more of: iptoasn peeringdb caida rir routeviews")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state = refresh_all(data_dir=args.data_dir, sources=args.sources, force=args.force)
    return 0 if not state.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
