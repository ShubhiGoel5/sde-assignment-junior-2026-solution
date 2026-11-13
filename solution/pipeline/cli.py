"""Command line:

    python -m solution.pipeline ingest data/drop_1
    python -m solution.pipeline report --out reports/
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .db import connect, sync_dispositions
from .ingest import IngestError, find_drops, ingest_drop
from .pii import load_vault
from .report import write_reports
from .schema import DEFAULT_SCHEMA_DIR, load_dispositions, load_schemas


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=Path("ledger.sqlite"),
                        help="SQLite database file (default: ledger.sqlite)")
    common.add_argument("--schemas", type=Path, default=DEFAULT_SCHEMA_DIR,
                        help="directory with <workflow>.json schemas and dispositions.csv")

    parser = argparse.ArgumentParser(prog="python -m solution.pipeline", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", parents=[common], help="ingest one or more drops")
    p.add_argument("paths", nargs="+", type=Path,
                   help="a drop directory, or a directory containing drops")
    p = sub.add_parser("report", parents=[common], help="write the five report files")
    p.add_argument("--out", type=Path, default=Path("reports"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.perf_counter()
    conn = connect(args.db, load_vault(args.db))
    try:
        sync_dispositions(conn, load_dispositions(args.schemas))
        if args.command == "ingest":
            schemas = load_schemas(args.schemas)
            for path in args.paths:
                for drop in find_drops(path):
                    print(f"ingesting {drop}", file=sys.stderr)
                    result = ingest_drop(conn, load_vault(args.db), schemas, drop)
                    print(f"  {result['ingested']} file(s) ingested, "
                          f"{result['skipped']} already ingested (skipped)", file=sys.stderr)
        else:
            write_reports(conn, args.out)
            print(f"reports written to {args.out}", file=sys.stderr)
    except IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"done in {time.perf_counter() - started:.1f}s", file=sys.stderr)
    return 0
