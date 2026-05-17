"""Four-direction ASN lookup API.

All functions return `AsnRecord` (or list thereof). The pydantic schema is
byte-for-byte compatible with the JSONL produced by ProjectDiscovery's
asnmap binary, so the CSTX parser in `cstx/src/cstx/plugins/easm/asnmap.py`
continues to work unchanged.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Callable, Iterable, Iterator, List, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .loader import AsnDataset, _IpRange, get_dataset

logger = logging.getLogger(__name__)


# ── Record schema ──────────────────────────────────────────────────────


class AsnRecord(BaseModel):
    """asnmap-compatible JSON record.

    Field names and types match ProjectDiscovery's asnmap binary so existing
    consumers (`ASNmapItem`, `asnmap_handler`) parse offline output identically.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    input: str = ""
    as_number: str = ""
    as_name: str = ""
    as_country: str = ""
    as_range: List[str] = Field(default_factory=list)


# ── Input classification ───────────────────────────────────────────────


class InputKind:
    IP = "ip"
    CIDR = "cidr"
    ASN = "asn"
    DOMAIN = "domain"
    ORG = "org"
    UNKNOWN = "unknown"


_ASN_RE = re.compile(r"^(?:AS|as)?(\d{1,10})$")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+\.?$"
)


def classify_input(value: str) -> str:
    """Heuristically determine which lookup direction an input belongs to."""
    text = (value or "").strip()
    if not text:
        return InputKind.UNKNOWN

    # CIDR
    if "/" in text:
        try:
            ipaddress.ip_network(text, strict=False)
            return InputKind.CIDR
        except ValueError:
            pass

    # Bare IP
    try:
        ipaddress.ip_address(text)
        return InputKind.IP
    except ValueError:
        pass

    # ASN ("AS13335" or "13335")
    m = _ASN_RE.match(text)
    if m:
        try:
            asn_int = int(m.group(1))
        except ValueError:
            asn_int = 0
        if 0 < asn_int < 2**32:
            return InputKind.ASN

    # Domain
    if _DOMAIN_RE.match(text):
        return InputKind.DOMAIN

    # Default: treat as organization free-text
    return InputKind.ORG


# ── Resolver abstraction for lookup_domain ─────────────────────────────


class Resolver(Protocol):
    def resolve(self, domain: str) -> List[str]: ...


class _SystemResolver:
    """`socket.getaddrinfo`-based resolver — honors container DNS config."""

    def resolve(self, domain: str) -> List[str]:
        try:
            infos = socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM)
        except (socket.gaierror, OSError) as exc:
            logger.debug("DNS resolution failed for %s: %s", domain, exc)
            return []
        ips: list[str] = []
        seen: set[str] = set()
        for info in infos:
            ip = info[4][0]
            if ip and ip not in seen:
                seen.add(ip)
                ips.append(ip)
        return ips


_default_resolver: Resolver = _SystemResolver()


# ── Builders ───────────────────────────────────────────────────────────


def _build_record_for_asn(
    dataset: AsnDataset,
    input_value: str,
    asn: int,
    *,
    fallback_country: str = "",
    fallback_name: str = "",
) -> AsnRecord:
    """Compose an AsnRecord from asn_meta + pyasn prefixes."""
    meta = dataset.asn_meta(asn)
    name = (meta["name"] if meta and meta["name"] else "") or fallback_name
    country = (meta["country"] if meta and meta["country"] else "") or fallback_country
    return AsnRecord(
        input=input_value,
        as_number=f"AS{asn}",
        as_name=name,
        as_country=country,
        as_range=dataset.get_as_prefixes(asn),
    )


def _resolve_asn_for_ip(
    dataset: AsnDataset, ip_str: str
) -> tuple[Optional[int], str, str]:
    """Return (asn, country_hint, name_hint) for an IP, using pyasn then IPtoASN."""
    asn, _prefix = dataset.lookup_pyasn(ip_str)
    if asn is not None and asn > 0:
        return asn, "", ""

    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return None, "", ""

    rng: Optional[_IpRange] = dataset.lookup_iptoasn(ip_obj)
    if rng is not None:
        return rng.asn, rng.country, rng.description
    return None, "", ""


# ── Public lookup functions ────────────────────────────────────────────


def lookup_ip(ip: str, *, dataset: Optional[AsnDataset] = None) -> AsnRecord:
    """Resolve an IPv4/IPv6 address to its ASN record.

    Returns an empty `AsnRecord(input=ip)` if the IP is not announced /
    not in IPtoASN — callers can detect this with `not record.as_number`.
    """
    ds = dataset or get_dataset()
    ip_clean = ip.strip()
    try:
        ipaddress.ip_address(ip_clean)
    except ValueError:
        return AsnRecord(input=ip_clean)

    asn, country_hint, name_hint = _resolve_asn_for_ip(ds, ip_clean)
    if asn is None:
        return AsnRecord(input=ip_clean)

    return _build_record_for_asn(
        ds,
        ip_clean,
        asn,
        fallback_country=country_hint,
        fallback_name=name_hint,
    )


def lookup_asn(asn: str | int, *, dataset: Optional[AsnDataset] = None) -> AsnRecord:
    """Resolve an ASN ("AS13335", "13335", or 13335) to metadata + prefixes."""
    ds = dataset or get_dataset()
    if isinstance(asn, int):
        asn_int = asn
        as_text = f"AS{asn}"
    else:
        as_text = asn.strip()
        m = _ASN_RE.match(as_text)
        if not m:
            return AsnRecord(input=as_text)
        asn_int = int(m.group(1))
        as_text = f"AS{asn_int}"

    return _build_record_for_asn(ds, as_text, asn_int)


def lookup_domain(
    domain: str,
    *,
    dataset: Optional[AsnDataset] = None,
    resolver: Optional[Resolver] = None,
) -> List[AsnRecord]:
    """Resolve a domain to one AsnRecord per distinct ASN of its A/AAAA records."""
    ds = dataset or get_dataset()
    r = resolver or _default_resolver
    name = domain.strip()
    if not name:
        return []
    ips = r.resolve(name)
    if not ips:
        return [AsnRecord(input=name)]

    seen_asn: set[int] = set()
    out: list[AsnRecord] = []
    for ip in ips:
        rec = lookup_ip(ip, dataset=ds)
        if not rec.as_number:
            continue
        try:
            asn_int = int(rec.as_number[2:]) if rec.as_number.startswith("AS") else int(rec.as_number)
        except ValueError:
            continue
        if asn_int in seen_asn:
            continue
        seen_asn.add(asn_int)
        # Replace `input` so the caller can correlate by the domain they asked for.
        out.append(rec.model_copy(update={"input": name}))

    if not out:
        out.append(AsnRecord(input=name))
    return out


def lookup_org(
    query: str,
    *,
    limit: int = 50,
    dataset: Optional[AsnDataset] = None,
) -> List[AsnRecord]:
    """Fuzzy-search organizations by name via FTS5, return their AsnRecords."""
    ds = dataset or get_dataset()
    text = query.strip()
    if not text:
        return []
    rows = ds.search_org(text, limit=limit)
    out: list[AsnRecord] = []
    for row in rows:
        try:
            asn_int = int(row["asn"])
        except (TypeError, ValueError):
            continue
        record = _build_record_for_asn(
            ds,
            text,
            asn_int,
            fallback_country=row["country"] or "",
            fallback_name=row["name"] or "",
        )
        out.append(record)
    return out


def lookup_many(
    values: Iterable[str],
    *,
    dataset: Optional[AsnDataset] = None,
    resolver: Optional[Resolver] = None,
    org_limit: int = 50,
) -> Iterator[AsnRecord]:
    """Auto-dispatch each input to the appropriate lookup and yield records."""
    ds = dataset or get_dataset()
    for raw in values:
        value = (raw or "").strip()
        if not value:
            continue
        kind = classify_input(value)
        if kind == InputKind.IP:
            yield lookup_ip(value, dataset=ds)
        elif kind == InputKind.CIDR:
            # Probe a representative address without expanding the CIDR. Some
            # data sources do not map the subnet address itself, so fall back to
            # the first address inside the block before returning a miss.
            try:
                net = ipaddress.ip_network(value, strict=False)
            except ValueError:
                yield AsnRecord(input=value)
                continue
            probe_addresses = [net.network_address]
            if net.num_addresses > 1:
                probe_addresses.append(net.network_address + 1)
            rec = AsnRecord(input=value)
            for address in probe_addresses:
                rec = lookup_ip(str(address), dataset=ds)
                if rec.as_number:
                    break
            yield rec.model_copy(update={"input": value})
        elif kind == InputKind.ASN:
            yield lookup_asn(value, dataset=ds)
        elif kind == InputKind.DOMAIN:
            yield from lookup_domain(value, dataset=ds, resolver=resolver)
        elif kind == InputKind.ORG:
            yield from lookup_org(value, limit=org_limit, dataset=ds)
        else:
            yield AsnRecord(input=value)
