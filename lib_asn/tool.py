"""In-process Tool adapter wrapping the offline ASN library.

`ASNmapOffline` subclasses the serverless `Tool` so the existing
`_run_tool(...)` plumbing in `plugins.easm.capabilities.serverless` works
unchanged, but the `execute` method short-circuits HTTP — every lookup is
served from the local dataset.

The CLI flag surface mirrors ProjectDiscovery's asnmap binary closely enough
that the same `ServerlessArgs` produced by `asnmap_baseline` works without
modification.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Union

from tools.serverless.matrix.tools.tool import (
    Tool,
    ToolRequest,
    ToolResponse,
    create_error_response,
)

from .loader import AsnDataNotReadyError, get_dataset
from .lookup import (
    AsnRecord,
    classify_input,
    InputKind,
    lookup_asn,
    lookup_domain,
    lookup_ip,
    lookup_many,
    lookup_org,
)

logger = logging.getLogger(__name__)


# Flags that take a value and are consumed in pairs.
_VALUE_FLAGS = {"-i", "-d", "-a", "-org", "-o", "-org-limit"}
# Boolean flags consumed standalone (asnmap-compat no-ops + json switches).
_BOOL_FLAGS = {"-j", "-json", "-silent", "-duc", "-v", "-verbose", "-nc", "-no-color"}


def _parse_cmd(cmd: List[str]) -> dict[str, list[str]]:
    """Extract recognized flag values from a ToolRequest.cmd list.

    Returns a dict with keys: inputs, domains, asns, orgs (lists of strings)
    and `org_limit` (int, default 50). Unknown flags are ignored — matching
    the lenient parsing the asnmap binary uses.
    """
    parsed: dict[str, Any] = {
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
            # consume the value even if we don't use it (e.g. -o output_file)
            i += 2
            continue
        # Treat bare positional tokens (no leading dash) as `-i` inputs —
        # matches asnmap's stdin / pipe usage where callers omit the flag.
        if not token.startswith("-"):
            parsed["inputs"].append(token)
        i += 1
    return parsed


def _dispatch(parsed: dict[str, Any]) -> list[AsnRecord]:
    out: list[AsnRecord] = []
    org_limit = parsed.get("org_limit", 50)

    for d in parsed["domains"]:
        out.extend(lookup_domain(d))
    for a in parsed["asns"]:
        out.append(lookup_asn(a))
    for o in parsed["orgs"]:
        out.extend(lookup_org(o, limit=org_limit))

    auto = parsed["inputs"]
    if auto:
        out.extend(lookup_many(auto, org_limit=org_limit))

    return out


class ASNmapOffline(Tool):
    """In-process replacement for the asnmap Go binary.

    Wires into `ServerlessCapability._TOOL_DEFS` under the name
    ``asnmap_offline``. The capability's `__getattr__` and `check_ability_ready`
    must skip endpoint validation when `IN_PROCESS = True`.
    """

    name = "asnmap_offline"
    json_flag = "-j"
    output_flags = ["-o"]
    input_flags: list[str] = []

    IN_PROCESS = True

    def execute(
        self,
        request: ToolRequest,
        json: str = "",
        *,
        endpoint: Union[str, List[str], None] = None,
        cancel_event: Any = None,
    ) -> ToolResponse:
        """Run a lookup against the local dataset; return asnmap-shaped response."""
        try:
            get_dataset()
        except AsnDataNotReadyError as exc:
            logger.error("ASN offline dataset not ready: %s", exc)
            return create_error_response(self.name, list(request.cmd), exc, returncode=503)

        parsed = _parse_cmd(list(request.cmd))
        # Older callers (e.g. parse_ips' IP enrichment) pass IPs via stdin
        # instead of repeated `-i` flags, matching how the Go binary accepts
        # newline-separated input. Treat stdin lines as auto-classified inputs.
        if request.stdin:
            try:
                text = request.stdin.decode("utf-8", errors="replace")
            except Exception:
                text = ""
            for line in text.splitlines():
                value = line.strip()
                if value:
                    parsed["inputs"].append(value)

        if not any(parsed[k] for k in ("inputs", "domains", "asns", "orgs")):
            return create_error_response(
                self.name,
                list(request.cmd),
                "no inputs provided (use -i / -d / -a / -org / stdin)",
                returncode=2,
            )

        if Tool._is_cancel_event_set(cancel_event):
            return create_error_response(self.name, list(request.cmd), "cancelled", returncode=499)

        try:
            records = _dispatch(parsed)
        except Exception as exc:
            logger.exception("ASN offline lookup failed")
            return create_error_response(self.name, list(request.cmd), exc, returncode=500)

        payload = [r.model_dump(by_alias=True) for r in records]
        body = "\n".join(r.model_dump_json(by_alias=True) for r in records)
        return ToolResponse(
            tool=self.name,
            cmd=list(request.cmd),
            stdout=body.encode("utf-8"),
            stderr=b"",
            returncode=0,
            success=True,
            jsonlines=payload if json else None,
        )
