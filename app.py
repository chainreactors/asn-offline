"""asn_offline FastAPI service — thin proxy producing asnmap-shaped JSONL.

Fans out each IP input to two backends:

1. ``iptoasn-webservice`` (jedisct1, internal container) for ASN number,
   AS description, and country code. URL is configured via ``IPTOASN_URL``.
2. Local ``pyasn`` instance loaded from ``ipasn.dat`` for the full announced
   prefix list of that ASN — this is what fills ``as_range`` in the CSTX
   parser (which yields one CIDR node per prefix).

The matrix ``ToolRequest`` / ``ToolResponse`` wire contract is preserved
so callers built against the legacy in-process ``ASNmapOffline`` continue
to work unchanged.

Argv surface is a strict subset of ProjectDiscovery asnmap: only ``-i`` /
positional inputs / stdin are honored. ``-d`` / ``-a`` / ``-org`` are no
longer supported (no production caller uses them; see ip_enrichment_flow
and asnmap_baseline — both feed IPs only).
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, List, Optional

import httpx
import pyasn
from fastapi import FastAPI, Response
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("asn_offline")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


IPTOASN_URL = os.environ.get("IPTOASN_URL", "http://iptoasn:53661").rstrip("/")
IPTOASN_TIMEOUT = float(os.environ.get("IPTOASN_TIMEOUT", "5.0"))
IPTOASN_JSON_HEADERS = {"Accept": "application/json"}
ASN_DATA_DIR = Path(os.environ.get("ASN_DATA_DIR", "/var/lib/asn_data"))


class AsnRecord(BaseModel):
    """asnmap-compatible JSON record — schema matches what CSTX
    ``ASNmapParser`` expects (`input`, `as_number`, `as_name`, `as_country`,
    `as_range`)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    input: str = ""
    as_number: str = ""
    as_name: str = ""
    as_country: str = ""
    as_range: List[str] = Field(default_factory=list)


# ── pyasn singleton (prefix lookup only) ──────────────────────────────


_PYASN_LOCK = threading.Lock()
_PYASN_DB: Optional[pyasn.pyasn] = None
_PYASN_MTIME: Optional[int] = None


class _PrefixDataNotReady(RuntimeError):
    pass


def _ipasn_path() -> Path:
    return ASN_DATA_DIR / "ipasn.dat"


def _load_pyasn(force: bool = False) -> pyasn.pyasn:
    """Load pyasn from ipasn.dat; hot-reload when the file mtime changes."""
    global _PYASN_DB, _PYASN_MTIME
    path = _ipasn_path()
    if not path.exists():
        raise _PrefixDataNotReady(f"ipasn.dat missing at {path}")
    try:
        mtime = path.stat().st_mtime_ns
    except OSError as exc:
        raise _PrefixDataNotReady(f"ipasn.dat stat failed: {exc}") from exc

    with _PYASN_LOCK:
        if not force and _PYASN_DB is not None and _PYASN_MTIME == mtime:
            return _PYASN_DB
        logger.info("loading pyasn radix from %s (mtime=%d)", path, mtime)
        _PYASN_DB = pyasn.pyasn(str(path))
        _PYASN_MTIME = mtime
        return _PYASN_DB


def _get_prefixes(asn: int) -> List[str]:
    try:
        db = _load_pyasn()
    except _PrefixDataNotReady as exc:
        logger.warning("pyasn not ready: %s", exc)
        return []
    try:
        prefixes = db.get_as_prefixes(asn)
    except Exception as exc:
        logger.debug("pyasn.get_as_prefixes(%s) failed: %r", asn, exc)
        return []
    if not prefixes:
        return []
    return sorted(prefixes)


# ── iptoasn-webservice client ─────────────────────────────────────────


_HTTP_CLIENT: Optional[httpx.Client] = None
_HTTP_LOCK = threading.Lock()


def _http() -> httpx.Client:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        return _HTTP_CLIENT
    with _HTTP_LOCK:
        if _HTTP_CLIENT is None:
            _HTTP_CLIENT = httpx.Client(timeout=IPTOASN_TIMEOUT)
        return _HTTP_CLIENT


def _query_iptoasn(ip: str) -> Optional[dict]:
    """Return iptoasn-webservice JSON for ``ip`` (announced=true), else None."""
    try:
        resp = _http().get(
            f"{IPTOASN_URL}/v1/as/ip/{ip}",
            headers=IPTOASN_JSON_HEADERS,
        )
    except httpx.HTTPError as exc:
        logger.warning("iptoasn lookup %s failed: %r", ip, exc)
        return None
    if resp.status_code != 200:
        logger.debug("iptoasn %s returned HTTP %d", ip, resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("announced"):
        return None
    return data


# ── Lookup ────────────────────────────────────────────────────────────


def _lookup_ip(ip: str) -> AsnRecord:
    ip_clean = ip.strip()
    if not ip_clean:
        return AsnRecord(input=ip)
    try:
        ipaddress.ip_address(ip_clean)
    except ValueError:
        return AsnRecord(input=ip_clean)

    data = _query_iptoasn(ip_clean)
    if data is None:
        return AsnRecord(input=ip_clean)

    asn_number_raw = data.get("as_number")
    try:
        asn_int = int(asn_number_raw) if asn_number_raw is not None else 0
    except (TypeError, ValueError):
        asn_int = 0
    if asn_int <= 0:
        return AsnRecord(input=ip_clean)

    return AsnRecord(
        input=ip_clean,
        as_number=f"AS{asn_int}",
        as_name=str(data.get("as_description") or ""),
        as_country=str(data.get("as_country_code") or ""),
        as_range=_get_prefixes(asn_int),
    )


# ── Argv parsing (IP-only subset of asnmap CLI) ──────────────────────


_VALUE_FLAGS_DROP = {"-d", "-a", "-org", "-o", "-org-limit"}
_BOOL_FLAGS_DROP = {"-j", "-json", "-silent", "-duc", "-v", "-verbose", "-nc", "-no-color"}


def _parse_inputs(cmd: List[str]) -> List[str]:
    """Extract IP inputs from a ToolRequest.cmd list.

    Honors ``-i <ip>`` and bare positional tokens. Older ASN/Domain/Org
    flags (``-d``/``-a``/``-org``) are silently dropped — no production
    caller emits them.
    """
    out: list[str] = []
    i = 0
    while i < len(cmd):
        token = cmd[i]
        if token == "-i" and i + 1 < len(cmd):
            out.append(cmd[i + 1])
            i += 2
            continue
        if token in _VALUE_FLAGS_DROP:
            i += 2
            continue
        if token in _BOOL_FLAGS_DROP:
            i += 1
            continue
        if not token.startswith("-"):
            out.append(token)
        i += 1
    return out


def _decode_stdin(value: Optional[str], encoding: Optional[str]) -> tuple[str, Optional[str]]:
    if not value:
        return "", None
    mode = (encoding or "base64").strip().lower()
    if mode in {"plain", "text", "raw"}:
        return value, None
    if mode not in {"base64", "b64"}:
        return "", f"unsupported stdin_encoding={encoding!r}"
    try:
        decoded = base64.b64decode(value.encode("utf-8"), validate=True)
    except (binascii.Error, ValueError):
        return "", "stdin must be base64-encoded"
    return decoded.decode("utf-8", errors="replace"), None


def _encode_bytes(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not value:
        return ""
    return base64.b64encode(value).decode("ascii")


# ── HTTP wire models ──────────────────────────────────────────────────


class FileFlag(BaseModel):
    flag: str
    files: Optional[dict[str, Optional[str]]] = None


class AsnOfflineInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    cmd: List[str] = Field(default_factory=list)
    stdin: Optional[str] = None
    stdin_encoding: Optional[str] = None
    invoke_id: Optional[str] = None
    files: Optional[List[FileFlag]] = None


class AsnOfflineInvokeResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    invoke_id: str = ""
    tool: str = "asnmap_offline"
    cmd: List[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    success: bool = True
    returncode: int = 0
    files: Optional[List[FileFlag]] = None
    jsonlines: Optional[List[dict]] = None
    error_message: Optional[str] = None


def _output_files(
    files: Optional[List[FileFlag]],
    stdout: bytes | str,
) -> Optional[List[FileFlag]]:
    if not files:
        return None
    content = _encode_bytes(stdout)
    out: list[FileFlag] = []
    for item in files:
        if item.flag != "-o" or not item.files:
            continue
        out.append(
            FileFlag(
                flag=item.flag,
                files={name: content for name in item.files},
            )
        )
    return out or None


def _response(
    req: AsnOfflineInvokeRequest,
    *,
    success: bool,
    returncode: int,
    stdout: bytes | str = b"",
    stderr: bytes | str = b"",
    jsonlines: Optional[List[dict[str, Any]]] = None,
    error_message: Optional[str] = None,
) -> AsnOfflineInvokeResponse:
    return AsnOfflineInvokeResponse(
        invoke_id=req.invoke_id or "",
        tool="asnmap_offline",
        cmd=list(req.cmd),
        stdout=_encode_bytes(stdout),
        stderr=_encode_bytes(stderr),
        success=success,
        returncode=returncode,
        files=_output_files(req.files, stdout),
        jsonlines=jsonlines,
        error_message=error_message,
    )


# ── FastAPI ──────────────────────────────────────────────────────────


app = FastAPI(title="asn_offline (iptoasn proxy + pyasn prefixes)")


@app.get("/health")
async def health(response: Response):
    """Liveness + dataset readiness probe.

    Reports prefix-data readiness (ipasn.dat present) and pings the
    iptoasn-webservice upstream. Returns 503 if either is unavailable so
    callers can soft-fail enrichment.
    """
    prefix_ok = _ipasn_path().exists()
    try:
        r = _http().get(
            f"{IPTOASN_URL}/v1/as/ip/8.8.8.8",
            headers=IPTOASN_JSON_HEADERS,
        )
        iptoasn_ok = r.status_code == 200
    except httpx.HTTPError:
        iptoasn_ok = False
    ok = iptoasn_ok  # prefix_data is optional; degrade gracefully
    if not ok:
        response.status_code = 503
    return {
        "ok": ok,
        "prefix_data_ready": prefix_ok,
        "iptoasn_reachable": iptoasn_ok,
        "ipasn_path": str(_ipasn_path()),
        "iptoasn_url": IPTOASN_URL,
    }


@app.post("/tools/asn_offline/invoke", response_model=AsnOfflineInvokeResponse)
async def invoke(req: AsnOfflineInvokeRequest):
    """Execute an asnmap-style lookup for each input IP."""
    inputs = _parse_inputs(list(req.cmd))

    stdin_text, stdin_error = _decode_stdin(req.stdin, req.stdin_encoding)
    if stdin_error:
        return _response(
            req,
            success=False,
            returncode=2,
            stderr=stdin_error,
            error_message=stdin_error,
        )
    if stdin_text:
        for line in stdin_text.splitlines():
            value = line.strip()
            if value:
                inputs.append(value)

    if not inputs:
        message = "no inputs provided (use -i / stdin)"
        return _response(
            req,
            success=False,
            returncode=2,
            stderr=message,
            error_message=message,
        )

    try:
        records = [_lookup_ip(ip) for ip in inputs]
    except Exception as exc:
        logger.exception("lookup failed")
        message = repr(exc)
        return _response(
            req,
            success=False,
            returncode=500,
            stderr=message,
            error_message=message,
        )

    payload = [r.model_dump(by_alias=True) for r in records]
    body = "\n".join(json.dumps(item, ensure_ascii=False) for item in payload)
    return _response(
        req,
        success=True,
        returncode=0,
        stdout=body,
        jsonlines=payload,
    )
