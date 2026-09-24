"""Ops CLI: ``python -m app.migrations [status|upgrade|check] [--db PATH] [--json]``.

status   read-only: schema version, applied/pending migrations, drift vs the models
upgrade  apply pending migrations (with backup + lock) and the additive reconcile
check    exit 0 when at head with no drift; 1 when pending/drift; 2 when the DB is newer
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .runner import MigrationError, SchemaTooNewError, check_not_too_new, discover, run_migrations, status


def _print_status(st: dict) -> None:
    print(f"database : {st['db_path']}{'' if st['exists'] else '  (does not exist yet)'}")
    print(f"schema   : v{st['current']}  (code head v{st['head']})")
    for row in st["applied"]:
        print(f"  applied  {row['version']:04d}_{row['name']:<34} {row['applied_at']}  app {row['app_version']}")
    for label in st["pending"]:
        print(f"  PENDING  {label}")
    for label in st.get("unknown", []):
        print(f"  UNKNOWN  {label}  (applied by a newer build)")
    for label in st.get("modified", []):
        print(f"  MODIFIED {label}  (file changed after it was applied)")
    drift = st.get("drift")
    if drift is None:
        return
    print("drift vs models:")
    empty = True
    for key in ("missing", "type_mismatches", "extra_columns", "nullability", "unsupported"):
        for item in drift[key]:
            empty = False
            print(f"  {key:<16} {item}")
    if empty:
        print("  none")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.migrations", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="status", choices=["status", "upgrade", "check"])
    ap.add_argument("--db", type=Path, help="database file (default: settings.DATABASE_PATH)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

    try:
        if args.command == "upgrade":
            report = run_migrations(args.db)
            if args.json:
                print(json.dumps(report, indent=2, default=str))
            else:
                print(f"upgraded {report['db_path']}: v{report['from_version']} -> v{report['to_version']}"
                      f" (applied {len(report['applied'])}: {', '.join(report['applied']) or '-'})")
                if report["backup"]:
                    print(f"backup: {report['backup']}")
                rec = report["reconcile"] or {}
                print(f"reconcile: added {len(rec.get('applied', []))}, errors {len(rec.get('errors', []))}")
            return 0 if not (report["reconcile"] or {}).get("errors") else 1

        st = status(args.db)
        if args.json:
            print(json.dumps(st, indent=2, default=str))
        else:
            _print_status(st)
        if args.command == "check":
            migrations = discover()
            check_not_too_new({a["version"]: a for a in st["applied"]}, migrations, st["db_path"])
            drift = st.get("drift") or {}
            bad = bool(st["pending"]) or bool(drift.get("missing") or drift.get("type_mismatches")
                                              or drift.get("unsupported"))
            print("check: " + ("FAIL (pending migrations or schema drift)" if bad else "OK"))
            return 1 if bad else 0
        return 0
    except SchemaTooNewError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
