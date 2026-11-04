"""Configure and run manually acquired feeds without enabling HTTP polling."""

import hashlib
import json
import uuid
from pathlib import Path

from election_pipeline.deployment import catalog, containers, control
from election_pipeline.settings import CollectorSettings


def configure(path, release, image, name, prod, config, max_age_seconds=21600):
    control.controls_ready(config)
    if not 900 <= max_age_seconds <= 604800:
        raise ValueError("Manual freshness limit must be between 15 minutes and seven days")
    identity, revision = containers.verify_release(release, image)
    meta = containers.metadata(identity, name)
    if meta["code_sha256"] != containers.package_hash(release):
        raise ValueError("Image Python sources differ from its release checkout")
    with control.connect(control.db_settings(prod, writer=True)) as conn:
        for migration in conn.execute("SELECT name, sha256 FROM ops.schema_migrations").fetchall():
            file = Path(release) / "migrations" / migration["name"]
            if (
                not file.is_file()
                or hashlib.sha256(file.read_bytes()).hexdigest() != migration["sha256"]
            ):
                raise ValueError("Manual release changed or omitted an applied migration")
    source = catalog.source_from_json(meta["source"])
    settings = CollectorSettings(**meta["settings"])
    if source.dataset != "election_results" or source.format != "bundle" or source.files:
        raise ValueError("Manual mode requires a single-file election-results bundle")

    def change(value):
        catalog.verify_baseline(value, prod)
        old = value["sources"][name]
        if old["worker"] not in ("expansion", "manual") or old["enabled"] or old["public"]:
            raise ValueError(
                "Only an existing disabled, private expansion source can be configured"
            )
        if meta["source"] != old["source"] or meta["races"] != old["races"]:
            raise ValueError("Manual mode cannot change source identity or watched races")
        entry = catalog.entry(
            source,
            settings,
            image_id=identity,
            revision=revision,
            release_path=Path(release).resolve(),
            worker="manual",
            status=old["status"],
        )
        entry.update(
            races=meta["races"],
            release_sha256=containers.release_hash(release),
            manual_max_age_seconds=max_age_seconds,
        )
        value["sources"][name] = entry

    catalog.update(path, change)
    return {"outcome": "configured", "source_id": name, "worker": "manual", "public": False}


def operate(
    path,
    name,
    prod,
    config,
    *,
    file=None,
    sha256=None,
    downloaded_at=None,
    publish=False,
    promote=False,
):
    # Keep long imports off the shared catalog lock. Only this source is serialized.
    lock = Path(config["CONTROL_STATE_DIR"]) / (name + "-manual-operation")
    with catalog.locked(lock, exclusive=True, nonblocking=True):
        entry = catalog.read(path)["sources"][name]
        if entry["worker"] != "manual" or entry["source"]["dataset"] != "election_results":
            raise ValueError("Configure this source for manual imports first")
        if promote:
            return control.promote(path, name, prod, config, allow_manual=True)
        if publish and not (entry["public"] and entry["enabled"]):
            raise ValueError(
                "Initial imports require preparation, promotion, restore and activation"
            )
        inbox = (Path(prod["DATA_DIR"]) / "inbox").resolve()
        relative = Path(file).resolve(strict=True).relative_to(inbox)
        args = [
            "scripts/run_manual_source.py",
            "--source",
            name,
            "--file",
            str(Path("/app/data/inbox") / relative),
            "--sha256",
            sha256,
            "--downloaded-at",
            downloaded_at,
            "--status",
            entry["status"],
        ]
        previous = entry.get("manual_snapshot", {}).get("source_generated_at")
        if previous:
            args.extend(["--minimum-generated-at", previous])
        if publish:
            args.append("--publish")
        output = containers.run_job(
            entry, prod, config, args, timeout=int(config.get("JOB_TIMEOUT", "240"))
        )
        rows = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        result = rows[-1]
        if result.get("source_id") != name or result["outcome"] not in (
            "validated",
            "succeeded",
            "already_current",
        ):
            raise ValueError("Manual worker did not produce a validated result")

        def change(value):
            current = value["sources"][name]
            if current["image_id"] != entry["image_id"] or current["worker"] != "manual":
                raise ValueError("Source deployment changed during import")
            receipt = result["manual_acquisition"]
            info = {
                key: receipt[key]
                for key in (
                    "downloaded_at",
                    "imported_at",
                    "source_generated_at",
                    "original_sha256",
                    "sha256",
                    "metadata_path",
                )
            }
            if result["outcome"] == "validated":
                current["candidate_id"] = str(uuid.UUID(result["candidate_id"]))
                current["manual_candidate"] = {**info, "candidate_id": current["candidate_id"]}
            else:
                current["publication_id"] = str(uuid.UUID(result["publication_id"]))
                current["manual_snapshot"] = {**info, "publication_id": current["publication_id"]}
                current["published_at"] = control.utc_now()
                current.pop("candidate_id", None)
                current.pop("manual_candidate", None)

        catalog.update(path, change)
        return result
