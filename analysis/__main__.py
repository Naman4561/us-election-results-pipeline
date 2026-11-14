"""python -m analysis restore | fetch counties|fec|census | link | match | build | open | status"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from analysis import build, candidates, census, fec, fetch, link, local, registry, restore


def status():
    db = local.start()
    with local.connect(db) as conn:
        latest = conn.execute(
            "SELECT backup_id, backup_finished_at, restore_checked, restored_at"
            " FROM ref.restores ORDER BY restored_at DESC LIMIT 1"
        ).fetchone()
        datasets = conn.execute(
            "SELECT name, vintage, downloaded_at FROM ref.datasets ORDER BY downloaded_at"
        ).fetchall()
    print(f"Local database: 127.0.0.1:{db['port']}/{db['dbname']} as {db['user']}")
    if latest:
        print(
            f"Restored from {latest['backup_id']} (backup finished {latest['backup_finished_at']},"
            f" restore-checked: {latest['restore_checked']}) at {latest['restored_at']}"
        )
    for row in datasets:
        print(f"Dataset {row['name']} ({row['vintage']}), downloaded {row['downloaded_at']}")


def main():
    parser = argparse.ArgumentParser(prog="python -m analysis", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("restore", help="Copy the newest verified backup into the local database")
    get = sub.add_parser("fetch", help="Download an outside dataset with its provenance")
    get.add_argument("dataset", choices=["counties", "fec", "census"])
    sub.add_parser("link", help="Match the results' county names to FIPS codes")
    sub.add_parser("match", help="Key every candidate and match Senate/House ones to the FEC")
    sub.add_parser("build", help="Build and test the analytics tables (dbt)")
    sub.add_parser("open", help="Open the workspace in the browser")
    sub.add_parser("status", help="Which backup and datasets the local database holds")
    args = parser.parse_args()

    if args.command == "restore":
        restore.main()
    elif args.command == "fetch":
        with local.connect(local.start()) as conn:
            local.apply_schemas(conn)
            loaders = {"counties": fetch.counties, "fec": fec.load, "census": census.load}
            print(loaders[args.dataset](conn))
    elif args.command == "match":
        with local.connect(local.start()) as conn:
            local.apply_schemas(conn)
            registry.sync(conn)
            fec.exclusions(conn)
            ids, ambiguous, missing = candidates.build(conn)
        print(f"{len(ids)} Senate/House candidates have an FEC ID")
        print(f"{sum(not agrees for *_, agrees in ids)} of them under a different party label")
        for label, people in (("Ambiguous", ambiguous), ("No FEC registration found", missing)):
            notable = [p for p in people if (p["vote_share"] or 0) >= 0.05]
            print(f"{label}: {len(people)} ({len(notable)} with 5%+ of the vote)")
            for p in notable:
                options = ", ".join(f"{r['cand_id']} {r['name']}" for r in p["found"])
                print(
                    f"  {p['contest_id']}  {p['choice']} ({p['party']}, {p['vote_share']:.1%})"
                    + (f": {options}" if options else "")
                )
    elif args.command == "link":
        with local.connect(local.start()) as conn:
            found, missing = link.link(conn)
        print(f"{len(found)} county spellings matched; {len(missing)} not matched")
        for state, county in missing:
            print(f"  {state}  {county}")
    elif args.command == "build":
        return build.build()
    elif args.command == "open":
        local.start()
        root = Path(__file__).resolve().parents[1]
        workspace = root / "analysis" / "workspace.py"
        env = {**os.environ, "PYTHONPATH": str(root)}
        return subprocess.call(
            [sys.executable, "-m", "marimo", "edit", str(workspace)], cwd=root, env=env
        )
    else:
        status()


if __name__ == "__main__":
    raise SystemExit(main())
