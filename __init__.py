"""asn_offline — IaC container that serves offline ASN / IP / Org lookups.

Replaces the in-process `lib.asn.ASNmapOffline` tool. Deployed as a
``kind: python_module`` tool in the IaC fleet (see ``meta.yaml``). The
container bundles ``lib.asn`` lookup code + a daily refresh sidecar that
maintains the dataset on a mounted volume.

This package is the **container side**; the mapping main repo talks to it
through ``capability.map_asn`` once Phase 2b cutover is complete. Until then
the in-process ``ASNmapOffline`` is still the live path.
"""
