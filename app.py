"""asn_offline FastAPI service — IaC fleet endpoint for ASN/IP/Org lookups.

Speaks the same argv contract as the upstream ProjectDiscovery ``asnmap`` Go
binary and the in-process ``ASNmapOffline`` wrapper. Input is matrix
``ToolRequest``-shaped JSON: the asnmap command tail (``-i``, ``-d``, ``-a``,
``-org`` plus bare positional inputs) and optional base64 ``stdin``. Output is
matrix ``ToolResponse``-shaped JSON with asnmap JSONL records in base64
``stdout``.

The lookup itself is delegated to bundled ``lib_asn`` (copied in by the
build process from the mapping main repo's ``src/lib/asn`` package).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, Response
from pydantic import BaseModel, ConfigDict, Field

# `lib_asn` is the in-container copy of `src/lib/asn`. The build context
# stages it under /app/lib_asn so the import name does not collide with
# the host repo's `lib.asn` (this container has no `lib` package).
from lib_asn.lookup import (
    AsnRecord,
    lookup_asn,
    lookup_domain,
    lookup_many,
    lookup_org,
)
from lib_asn.loader import AsnDataNotReadyError, get_dataset
from settings import ASN_DATA_DIR

logger = logging.getLogger("asn_offline")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(title="asn_offline (offline ASN lookup)")

_DATASET_LOCK = threading.Lock()
_DATASET_IDENTITY: tuple[str, int | None] | None = None


# Argv parsing mirrors src/lib/asn/tool.py:_parse_cmd so callers built
# against the in-process ASNmapOffline are wire-compatible.
_VALUE_FLAGS = {"-i", "-d", "-a", "-org", "-o", "-org-limit"}
_BOOL_FLAGS = {"-j", "-json", "-silent", "-duc", "-v", "-verbose", "-nc", "-no-color"}


def _parse_cmd(cmd: List[str]) -> dict:
    parsed = {
        "inputs": [],
        "domains": [],
        "asns": [],
        "orgs": [],
        "org_limit": 50,
    }
    i = 0
    while i < len(cmd):
        token = cmd[i]
        if token in _BOOL_FLAGS:
            i += 1
            continue
        if token == "-i" and i + 1 < len(cmd):
            parsed["inputs"].append(cmd[i + 1])
            i += 2
            continue
        if token == "-d" and i + 1 < len(cmd):
            parsed["domains"].append(cmd[i + 1])
            i += 2
            continue
        if token == "-a" and i + 1 < len(cmd):
            parsed["asns"].append(cmd[i + 1])
            i += 2
            continue
        if token == "-org" and i + 1 < len(cmd):
            parsed["orgs"].append(cmd[i + 1])
            i += 2
            continue
        if token == "-org-limit" and i + 1 < len(cmd):
            try:
                parsed["org_limit"] = int(cmd[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if token in _VALUE_FLAGS:
            i += 2
            continue
        if not token.startswith("-"):
            parsed["inputs"].append(token)
        i += 1
    return parsed


def _dispatch(parsed: dict) -> List[AsnRecord]:
    out: List[AsnRecord] = []
    org_limit = parsed.get("org_limit", 50)
    for d in parsed["domains"]:
        out.extend(lookup_domain(d))
    for a in parsed["asns"]:
        out.append(lookup_asn(a))
    for o in parsed["orgs"]:
        out.extend(lookup_org(o, limit=org_limit))
    if parsed["inputs"]:
        out.extend(lookup_many(parsed["inputs"], org_limit=org_limit))
    return out


def _encode_bytes(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not value:
        return ""
    return base64.b64encode(value).decode("ascii")


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


def _dataset_identity() -> tuple[str, int | None]:
    data_dir = Path(os.environ.get("ASN_DATA_DIR") or ASN_DATA_DIR)
    resolved = data_dir.resolve(strict=False)
    manifest = resolved / "MANIFEST.json"
    try:
        manifest_mtime = manifest.stat().st_mtime_ns
    except OSError:
        manifest_mtime = None
    return str(resolved), manifest_mtime


def _load_dataset():
    """Load the singleton, reloading when dataset.current points elsewhere."""
    global _DATASET_IDENTITY
    identity = _dataset_identity()
    with _DATASET_LOCK:
        reload_dataset = _DATASET_IDENTITY is not None and identity != _DATASET_IDENTITY
        dataset = get_dataset(reload=reload_dataset)
        _DATASET_IDENTITY = identity
        return dataset


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


@app.get("/health")
async def health(response: Response):
    """Liveness + dataset readiness probe."""
    try:
        _load_dataset()
        ready = True
        detail = "dataset loaded"
    except AsnDataNotReadyError as exc:
        ready = False
        detail = str(exc)
        response.status_code = 503
    return {"ok": ready, "dataset_ready": ready, "detail": detail}


@app.post("/tools/asn_offline/invoke", response_model=AsnOfflineInvokeResponse)
async def invoke(req: AsnOfflineInvokeRequest):
    """Execute an asnmap-style lookup.

    Inputs from ``req.cmd`` (parsed for -i/-d/-a/-org) plus optional base64
    ``req.stdin`` lines (treated as auto-classified inputs, matching how the
    Go binary accepts stdin). Returns a matrix ToolResponse-shaped body.
    """
    try:
        _load_dataset()
    except AsnDataNotReadyError as exc:
        message = f"dataset not ready: {exc}"
        return _response(
            req,
            success=False,
            returncode=503,
            stderr=message,
            error_message=message,
        )

    parsed = _parse_cmd(list(req.cmd))
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
                parsed["inputs"].append(value)

    if not any(parsed[k] for k in ("inputs", "domains", "asns", "orgs")):
        message = "no inputs provided (use -i / -d / -a / -org / stdin)"
        return _response(
            req,
            success=False,
            returncode=2,
            stderr=message,
            error_message=message,
        )

    try:
        records = _dispatch(parsed)
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
