"""python -m analysis restore | fetch counties | link | open | status"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from analysis import fetch, link, local, restore


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
    get.add_argument("dataset", choices=["counties"])
    sub.add_parser("link", help="Match the results' county names to FIPS codes")
    sub.add_parser("open", help="Open the workspace in the browser")
    sub.add_parser("status", help="Which backup and datasets the local database holds")
    args = parser.parse_args()

    if args.command == "restore":
        restore.main()
    elif args.command == "fetch":
        with local.connect(local.start()) as conn:
            print(fetch.counties(conn))
    elif args.command == "link":
        with local.connect(local.start()) as conn:
            found, missing = link.link(conn)
        print(f"{len(found)} county spellings matched; {len(missing)} not matched")
        for state, county in missing:
            print(f"  {state}  {county}")
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
