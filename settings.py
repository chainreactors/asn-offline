"""Minimal settings module for the asn_offline container.

``lib_asn.loader.get_dataset()`` does a late ``from settings import
ASN_DATA_DIR`` to avoid pulling the mapping main repo's heavy settings
graph. This container has no mapping settings — we satisfy the import
with a thin env-driven module.

The container always writes / reads through ``$ASN_DATA_DIR``, which the
entrypoint points at the ``dataset.current`` symlink so the lookup
process sees a fresh dataset after every successful refresh swap.
"""
from __future__ import annotations

import os

ASN_DATA_DIR = os.environ.get(
    "ASN_DATA_DIR", "/var/lib/asn_offline/dataset.current"
)

# Days after which a loaded dataset is considered stale (loader logs a
# warning, but lookups continue to serve). Refresh sidecar should always
# beat this threshold.
ASN_STALE_DAYS = int(os.environ.get("ASN_STALE_DAYS", "3"))
