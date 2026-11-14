# AWS infrastructure

Production is one Lightsail instance in `us-east-2` (2 vCPU, 3.7 GiB RAM, 77 GB disk)
running Docker, PostgreSQL and systemd timers. AWS services around it handle
delivery (Lightsail CDN), backups (S3), alerts (SNS) and liveness (CloudWatch).

## Services

```mermaid
flowchart TB
    U(["Readers"]) --> CDN["Lightsail CDN distribution<br/>assets max-age 300, /api no-store"]
    CDN --> CADDY

    subgraph LS["Lightsail instance · Ubuntu"]
        subgraph compose["Docker Compose project election-production"]
            CADDY["caddy:2<br/>TLS · GET/HEAD only ·<br/>cache headers"]
            DB[("db · postgres:17<br/>volume postgres_data")]
        end
        subgraph owned["Started by scripts/deploy.py from the release image"]
            JOBS["election-job-*<br/>one per source run<br/>CPU/RAM capped, read-only fs"]
            DASH["election-dashboard-*<br/>active + previous"]
        end
        TIMERS["systemd timers<br/>worker · monitor · backup"]
        DATA[("/srv/election-pipeline<br/>data · backups")]
    end

    CADDY --> DASH
    DASH --> DB
    JOBS --> DB
    JOBS --> DATA
    TIMERS --> JOBS

    TIMERS -- backup --> S3[("S3 bucket<br/>election-pipeline/rolling/")]
    TIMERS -- alerts --> SNS["SNS topic<br/>alerts and recoveries"]
    TIMERS -- heartbeat --> CW["CloudWatch metric<br/>ElectionPipeline/MonitorHeartbeat"]

    OPS(["Operator laptop"]) -- SSH --> LS
    GH["GitHub"] -- "git clone pinned commit" --> LS
```

## Releases on the host

- `/opt/election-releases/<commit>`: an immutable checkout with its control venv; the
  image `election-pipeline:<commit>` is built from it and tested inside itself.
- `/opt/election-releases/current`: the symlink the systemd units run from.
- `/var/lib/election-pipeline-deployment/catalog.json`: the current and previous
  release, the dashboards, and each source's flags.
- Jobs run with `--read-only`, `--cap-drop ALL`, a non-root user, PID limits and the
  `JOB_CPUS` / `JOB_MEMORY` caps. Candidate dashboards start privately on
  `127.0.0.1:<port>`; `activate` switches Caddy and keeps the previous for rollback.

## Access and credentials

```mermaid
flowchart LR
    subgraph roles["PostgreSQL roles"]
        W["writer<br/>pipeline, jobs"]
        RD["reader<br/>dashboard: serving + published tables"]
    end
    subgraph aws["AWS IAM user election-pipeline-backup"]
        A1["S3 upload, download, list"]
        A2["s3:PutObjectTagging on rolling/*<br/>(to add for retention)"]
        A3["SNS publish · CloudWatch metric"]
        A4["no s3:DeleteObject (verified)"]
    end
    ENV["/etc/election-pipeline/*.env<br/>root-only, never in images"]
    ENV --> W & RD & aws
```

The backup user cannot delete. Old backups are removed by a bucket lifecycle rule
once tagged, so a compromised host cannot erase recent backups.

## Backups to S3

```mermaid
sequenceDiagram
    autonumber
    participant T as Backup timer (00/6:08 UTC)
    participant B as production_recovery.py
    participant PG as PostgreSQL
    participant D as Host disk
    participant S3 as S3 rolling/
    participant LC as Lifecycle rule

    T->>B: start
    B->>B: Lock every source (skip if a job is running)
    B->>PG: Repeatable-read snapshot + dump
    B->>D: Copy data dirs, verify every raw file hash
    B->>D: bundle.zip (~4 GB)
    B->>S3: bundle.zip, then complete.json
    B->>D: uploaded.json, delete the unpacked copy (keep bundle.zip)
    B->>D: prune to newest 1
    B->>S3: Tag backups outside newest 5 retention=expire
    LC->>S3: Delete tagged objects older than 3 days
```

Details and the one-time IAM and lifecycle setup are in [../backups.md](../backups.md).
