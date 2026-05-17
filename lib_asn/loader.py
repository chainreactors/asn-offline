"""Dataset loader and singleton for the offline ASN library.

The dataset is built by `workflow.jobs.asn_refresh` into `ASN_DATA_DIR` with
this layout:

    <ASN_DATA_DIR>/
        ipasn.dat               # pyasn radix (Routeviews MRT)
        ip2asn-combined.tsv     # IPtoASN raw, loaded into in-memory bisect
        asn_meta.sqlite         # ASN→{name,country,org_id} + org_fts FTS5
        peeringdb_net.json      # raw, used at refresh time
        caida_as_org.txt        # raw, used at refresh time
        MANIFEST.json

`AsnDataset.load_or_raise(data_dir)` is the only entry point; everything
downstream goes through `get_dataset()` which memoizes a singleton.

Thread-safety: the singleton load is guarded by a lock; pyasn and the bisect
lists are read-only after load, sqlite connections are per-thread.
"""

from __future__ import annotations

import bisect
import ipaddress
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AsnDataNotReadyError(RuntimeError):
    """Raised when the dataset directory is missing required artifacts."""


class AsnDatasetStaleWarning(UserWarning):
    """Raised via logger when the manifest is older than configured threshold."""


# ── Manifest ────────────────────────────────────────────────────────────


class _SourceManifest(BaseModel):
    generated_at: Optional[datetime] = None
    sha256: Optional[str] = None
    record_count: Optional[int] = None


class AsnManifest(BaseModel):
    """Top-level dataset manifest written by `asn_refresh`."""

    generated_at: datetime
    sources: dict[str, _SourceManifest] = Field(default_factory=dict)

    def is_stale(self, threshold_days: int) -> bool:
        if threshold_days <= 0:
            return False
        age = datetime.now(timezone.utc) - self.generated_at.astimezone(timezone.utc)
        return age.days > threshold_days

    @classmethod
    def from_path(cls, path: Path) -> "AsnManifest":
        if not path.exists():
            raise AsnDataNotReadyError(f"MANIFEST.json missing at {path}")
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise AsnDataNotReadyError(f"MANIFEST.json invalid: {exc}") from exc


# ── IPtoASN bisect radix ────────────────────────────────────────────────


@dataclass(frozen=True)
class _IpRange:
    """One range from ip2asn-combined.tsv (already normalized to ints)."""

    start: int
    end: int
    asn: int
    country: str
    description: str


class _IptoasnRadix:
    """Bisect-based IP-range lookup over IPtoASN's combined TSV.

    Two parallel sorted lists (v4 and v6) of `_IpRange`. Lookup is one
    bisect (O(log n)) plus a single bounds check.
    """

    __slots__ = ("_v4", "_v4_starts", "_v6", "_v6_starts")

    def __init__(self) -> None:
        self._v4: List[_IpRange] = []
        self._v4_starts: List[int] = []
        self._v6: List[_IpRange] = []
        self._v6_starts: List[int] = []

    @classmethod
    def load(cls, tsv_path: Path) -> "_IptoasnRadix":
        radix = cls()
        if not tsv_path.exists():
            logger.warning("IPtoASN TSV missing at %s — IPtoASN fallback disabled", tsv_path)
            return radix

        v4: List[_IpRange] = []
        v6: List[_IpRange] = []
        with tsv_path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                # combined TSV: range_start, range_end, AS_number, country_code, AS_description
                if len(parts) < 5:
                    continue
                start_s, end_s, asn_s, country, desc = parts[0], parts[1], parts[2], parts[3], parts[4]
                try:
                    asn = int(asn_s)
                except ValueError:
                    continue
                if asn == 0:
                    # IPtoASN uses 0 for "not routed" — skip; pyasn miss should
                    # remain a miss rather than be hidden by this fallback.
                    continue
                try:
                    start_ip = ipaddress.ip_address(start_s)
                    end_ip = ipaddress.ip_address(end_s)
                except ValueError:
                    continue
                rng = _IpRange(int(start_ip), int(end_ip), asn, country, desc)
                if start_ip.version == 4:
                    v4.append(rng)
                else:
                    v6.append(rng)

        v4.sort(key=lambda r: r.start)
        v6.sort(key=lambda r: r.start)
        radix._v4 = v4
        radix._v4_starts = [r.start for r in v4]
        radix._v6 = v6
        radix._v6_starts = [r.start for r in v6]
        logger.info("IPtoASN radix loaded: v4=%d v6=%d", len(v4), len(v6))
        return radix

    def lookup(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Optional[_IpRange]:
        ip_int = int(ip)
        if ip.version == 4:
            ranges, starts = self._v4, self._v4_starts
        else:
            ranges, starts = self._v6, self._v6_starts
        if not starts:
            return None
        idx = bisect.bisect_right(starts, ip_int) - 1
        if idx < 0:
            return None
        rng = ranges[idx]
        if rng.start <= ip_int <= rng.end:
            return rng
        return None


# ── Dataset singleton ──────────────────────────────────────────────────


class AsnDataset:
    """Loaded ASN dataset — pyasn radix + IPtoASN bisect + asn_meta sqlite."""

    def __init__(
        self,
        data_dir: Path,
        manifest: AsnManifest,
        pyasn_db: object,  # pyasn.pyasn instance
        iptoasn: _IptoasnRadix,
        sqlite_path: Path,
    ) -> None:
        self.data_dir = data_dir
        self.manifest = manifest
        self._pyasn = pyasn_db
        self._iptoasn = iptoasn
        self._sqlite_path = sqlite_path
        self._tls = threading.local()

    # -- pyasn proxies -------------------------------------------------

    def lookup_pyasn(self, ip: str) -> Tuple[Optional[int], Optional[str]]:
        """Return (asn, matched_prefix) from pyasn, or (None, None) on miss."""
        try:
            asn, prefix = self._pyasn.lookup(ip)
        except Exception:
            return None, None
        return asn, prefix

    def get_as_prefixes(self, asn: int) -> List[str]:
        try:
            prefixes = self._pyasn.get_as_prefixes(asn)
        except Exception:
            return []
        return sorted(prefixes) if prefixes else []

    # -- IPtoASN fallback ----------------------------------------------

    def lookup_iptoasn(
        self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address
    ) -> Optional[_IpRange]:
        return self._iptoasn.lookup(ip)

    # -- asn_meta sqlite -----------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            uri = f"file:{self._sqlite_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._tls.conn = conn
        return conn

    def asn_meta(self, asn: int) -> Optional[sqlite3.Row]:
        try:
            row = self._conn().execute(
                "SELECT asn, name, country, org_id, source FROM asn_meta WHERE asn = ?",
                (asn,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            logger.debug("asn_meta query failed (asn=%s): %s", asn, exc)
            return None
        return row

    def search_org(self, query: str, limit: int = 50) -> List[sqlite3.Row]:
        """Return asn_meta rows matching FTS5 query, ranked by bm25."""
        if not query or not query.strip():
            return []
        fts_query = _build_fts_query(query)
        try:
            rows = self._conn().execute(
                """
                SELECT m.asn AS asn, m.name AS name, m.country AS country,
                       m.org_id AS org_id, m.source AS source
                FROM org_fts f
                JOIN asn_meta m ON m.asn = f.asn
                WHERE org_fts MATCH ?
                ORDER BY bm25(org_fts)
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("org FTS query failed: %s", exc)
            return []
        return list(rows)

    # -- Construction --------------------------------------------------

    @classmethod
    def load_or_raise(cls, data_dir: Path) -> "AsnDataset":
        try:
            import pyasn  # type: ignore
        except ImportError as exc:
            raise AsnDataNotReadyError(
                "pyasn is not installed; add it to pyproject.toml dependencies."
            ) from exc

        data_dir = Path(data_dir).resolve()
        if not data_dir.exists():
            raise AsnDataNotReadyError(f"ASN_DATA_DIR does not exist: {data_dir}")

        manifest = AsnManifest.from_path(data_dir / "MANIFEST.json")

        ipasn_path = data_dir / "ipasn.dat"
        if not ipasn_path.exists():
            raise AsnDataNotReadyError(f"ipasn.dat missing at {ipasn_path}")
        pyasn_db = pyasn.pyasn(str(ipasn_path))

        sqlite_path = data_dir / "asn_meta.sqlite"
        if not sqlite_path.exists():
            raise AsnDataNotReadyError(f"asn_meta.sqlite missing at {sqlite_path}")

        iptoasn = _IptoasnRadix.load(data_dir / "ip2asn-combined.tsv")

        return cls(
            data_dir=data_dir,
            manifest=manifest,
            pyasn_db=pyasn_db,
            iptoasn=iptoasn,
            sqlite_path=sqlite_path,
        )


# ── Singleton plumbing ────────────────────────────────────────────────

_singleton_lock = threading.Lock()
_singleton: Optional[AsnDataset] = None


def get_dataset(data_dir: Optional[Path] = None, reload: bool = False) -> AsnDataset:
    """Return the process-wide AsnDataset singleton.

    `data_dir` is resolved from `settings.ASN_DATA_DIR` when omitted. Pass
    `reload=True` after a refresh job finishes to drop the in-memory cache.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is not None and not reload:
            return _singleton
        if data_dir is None:
            from settings import ASN_DATA_DIR  # late import to avoid settings cycles

            data_dir = ASN_DATA_DIR
        dataset = AsnDataset.load_or_raise(Path(data_dir))
        try:
            from settings import ASN_STALE_DAYS

            if dataset.manifest.is_stale(ASN_STALE_DAYS):
                logger.error(
                    "ASN dataset is stale: generated_at=%s threshold_days=%d — "
                    "schedule asn_refresh job",
                    dataset.manifest.generated_at.isoformat(),
                    ASN_STALE_DAYS,
                )
        except Exception:
            pass
        _singleton = dataset
        return dataset


def _build_fts_query(raw: str) -> str:
    """Sanitize a free-form org-name query for FTS5 MATCH.

    Tokens are quoted to avoid FTS5 operator parsing; multiple tokens are
    AND-joined. Wildcard suffix is appended so short queries still match.
    """
    tokens = [tok for tok in raw.split() if tok.strip()]
    if not tokens:
        return ""
    quoted = []
    for tok in tokens:
        cleaned = tok.replace('"', "").strip()
        if not cleaned:
            continue
        quoted.append(f'"{cleaned}"*')
    return " ".join(quoted)
