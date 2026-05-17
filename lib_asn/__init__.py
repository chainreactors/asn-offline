"""Offline ASN / IP / Organization lookup library.

.. deprecated:: 2026-05-14
    ``lib.asn`` is deprecated and slated to be moved out of the mapping main
    repo into an independent IaC tool container (``tools/asnmap-offline/``).
    Reason: 1000 LOC + 200MB dataset + periodic refresh job belong on the
    tool side per ``docs/guides/capability-boundary.md`` ("整改进行中" section).

    **Do not add new imports of this module.** New ASN needs must go through
    ``capability.map_asn`` — that call site is stable across the migration
    (it currently routes to in-process ``ASNmapOffline`` but will route to
    the container after Phase 2). Callers that already import ``lib.asn``
    will be migrated as part of Phase 3.

Replaces the chaos-API-backed asnmap binary with a self-hosted dataset
built from Routeviews (pyasn), IPtoASN, PeeringDB and CAIDA. See
``src/workflow/jobs/asn_refresh.py`` for the data-ingest pipeline.

Public API:
    AsnRecord        — Pydantic record mirroring asnmap JSONL schema
    lookup_ip        — IP (v4/v6) → AsnRecord
    lookup_domain    — Domain → list[AsnRecord]
    lookup_asn       — ASN → AsnRecord
    lookup_org       — Org name (fuzzy) → list[AsnRecord]
    lookup_many      — Mixed iterable; auto-detect input kind
    get_dataset      — Internal singleton accessor (rarely used directly)
    AsnDataNotReadyError — Raised when the dataset directory is missing/empty
"""
import warnings as _warnings

from .loader import AsnDataNotReadyError, AsnDataset, AsnManifest, get_dataset
from .lookup import (
    AsnRecord,
    classify_input,
    lookup_asn,
    lookup_domain,
    lookup_ip,
    lookup_many,
    lookup_org,
)

_warnings.warn(
    "lib.asn is deprecated and will move to the asnmap-offline IaC container; "
    "new code should call capability.map_asn instead. "
    "See docs/guides/capability-boundary.md (\"整改进行中\" section).",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "AsnRecord",
    "AsnDataset",
    "AsnDataNotReadyError",
    "AsnManifest",
    "classify_input",
    "get_dataset",
    "lookup_asn",
    "lookup_domain",
    "lookup_ip",
    "lookup_many",
    "lookup_org",
]
