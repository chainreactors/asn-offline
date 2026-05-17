"""CLI surface for the offline ASN library.

Invoke via ``python -m lib.asn``. Output matches ProjectDiscovery asnmap's
JSONL schema so it can drop into any pipeline that already consumes asnmap.

Recognized flags (a strict subset of asnmap's; unknowns are tolerated):

    -i <value>           Auto-detect input kind. Repeatable. (Also reads stdin.)
    -d <domain>          Force domain lookup. Repeatable.
    -a <asn>             Force ASN lookup. Repeatable.
    -org <name>          Force organization fuzzy search. Repeatable.
    -j / -json           JSONL output (default).
    -silent              Suppress non-data log output.
    -duc                 asnmap compatibility no-op.
    -o <file>            Write output to file in addition to stdout.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Iterable, List

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


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lib.asn",
        description="Offline ASN lookup (asnmap-compatible JSONL output).",
        add_help=True,
    )
    p.add_argument("-i", dest="inputs", action="append", default=[])
    p.add_argument("-d", dest="domains", action="append", default=[])
    p.add_argument("-a", dest="asns", action="append", default=[])
    p.add_argument("-org", dest="orgs", action="append", default=[])
    p.add_argument("-o", dest="output_file", default=None)
    p.add_argument("-j", "-json", dest="json_flag", action="store_true")
    p.add_argument("-silent", dest="silent", action="store_true")
    p.add_argument("-duc", dest="duc", action="store_true")
    p.add_argument("-org-limit", dest="org_limit", type=int, default=50)
    return p


def _iter_stdin() -> Iterable[str]:
    if sys.stdin is None:
        return ()
    try:
        if sys.stdin.isatty():
            return ()
        return [line.strip() for line in sys.stdin if line.strip()]
    except OSError:
        return ()


def _run(args: argparse.Namespace) -> List[AsnRecord]:
    out: list[AsnRecord] = []

    for d in args.domains:
        out.extend(lookup_domain(d))
    for a in args.asns:
        out.append(lookup_asn(a))
    for o in args.orgs:
        out.extend(lookup_org(o, limit=args.org_limit))

    auto_inputs = list(args.inputs)
    auto_inputs.extend(_iter_stdin())
    if auto_inputs:
        out.extend(lookup_many(auto_inputs, org_limit=args.org_limit))

    return out


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.silent else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    records = _run(args)
    lines = [r.model_dump_json(by_alias=True) for r in records]
    body = "\n".join(lines)
    if body:
        sys.stdout.write(body + "\n")
        sys.stdout.flush()

    if args.output_file:
        Path(args.output_file).write_text(
            body + ("\n" if body else ""), encoding="utf-8"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
