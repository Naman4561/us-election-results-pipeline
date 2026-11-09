"""Manually acquired official files: imported by an operator instead of HTTP polling."""

import json
import uuid
from pathlib import Path

from election_pipeline.deployment import catalog, containers, control


def configure(path, name, max_age_seconds=21600, final=False):
    """Switch a disabled, private results source to manual imports."""
    if not 900 <= max_age_seconds <= 604800:
        raise ValueError("Manual freshness limit must be between 15 minutes and seven days")
    source = catalog.configured()[name]
    if source.dataset != "election_results" or source.format != "bundle" or source.files:
        raise ValueError("Manual mode requires a single-file election-results bundle")

    def change(value):
        entry = value["sources"][name]
        if entry["enabled"] or entry["public"]:
            raise ValueError("Only a disabled, private source can be configured")
        entry.update(
            acquisition="manual",
            manual_max_age_seconds=max_age_seconds,
            # The issuer has stopped updating its unofficial results; no import is due.
            manual_final=final is True,
        )

    catalog.update(path, change)
    return {"outcome": "configured", "source_id": name, "acquisition": "manual", "public": False}


RECEIPT = (
    "downloaded_at",
    "imported_at",
    "source_generated_at",
    "original_sha256",
    "sha256",
    "metadata_path",
)


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
        value = catalog.read(path)
        entry = value["sources"][name]
        if entry["acquisition"] != "manual":
            raise ValueError("Configure this source for manual imports first")
        if promote:
            return control.promote(path, name, prod, config)
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
        image = value["release"]["image_id"]
        output = containers.run_job(
            image, prod, config, args, timeout=int(config.get("JOB_TIMEOUT", "240"))
        )
        result = [json.loads(line) for line in output.splitlines() if line.startswith("{")][-1]
        if result.get("source_id") != name or result["outcome"] not in (
            "validated",
            "succeeded",
            "already_current",
        ):
            raise ValueError("Manual worker did not produce a validated result")

        def change(current):
            source = current["sources"][name]
            if current["release"]["image_id"] != image or source["acquisition"] != "manual":
                raise ValueError("Deployment changed during import")
            info = {key: result["manual_acquisition"][key] for key in RECEIPT}
            if result["outcome"] == "validated":
                source["candidate_id"] = str(uuid.UUID(result["candidate_id"]))
                source["manual_candidate"] = {**info, "candidate_id": source["candidate_id"]}
            else:
                source["publication_id"] = str(uuid.UUID(result["publication_id"]))
                source["manual_snapshot"] = {**info, "publication_id": source["publication_id"]}
                source["published_at"] = control.utc_now()
                source.pop("candidate_id", None)
                source.pop("manual_candidate", None)

        catalog.update(path, change)
        return result
