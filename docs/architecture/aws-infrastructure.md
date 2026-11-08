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
            DASH["dashboard<br/>FastAPI, read-only DB role"]
            DB[("db · postgres:17<br/>volume postgres_data")]
            PIPE["pipeline job<br/>baseline sources NC NE"]
        end
        subgraph expansion["Managed by scripts/deploy.py"]
            JOBS["election-expansion-job-*<br/>one per source run<br/>CPU/RAM capped, read-only fs"]
            CAND["election-expansion-dashboard-*<br/>active + previous candidates"]
        end
        TIMERS["systemd timers<br/>pipeline · expansion · monitor · backup"]
        DATA[("/srv/election-pipeline<br/>data · backups")]
    end

    CADDY --> DASH & CAND
    DASH & CAND --> DB
    PIPE & JOBS --> DB
    PIPE & JOBS --> DATA
    TIMERS --> PIPE & JOBS

    TIMERS -- backup --> S3[("S3 bucket<br/>election-pipeline/rolling/")]
    TIMERS -- alerts --> SNS["SNS topic<br/>alerts and recoveries"]
    TIMERS -- heartbeat --> CW["CloudWatch metric<br/>ElectionPipeline/MonitorHeartbeat"]

    OPS(["Operator laptop"]) -- SSH --> LS
    GH["GitHub"] -- "git clone pinned commit" --> LS
```

## Isolation on the host

```mermaid
flowchart LR
    subgraph baseline["Baseline (accepted release)"]
        B1["/opt/election-pipeline<br/>image IMAGE_TAG"]
        B2["NC, NE sources"]
    end
    subgraph releases["/opt/election-releases/{commit}"]
        R1["Control release<br/>CONTROL_ROOT"]
        R2["Pinned releases<br/>one image per source"]
    end
    subgraph catalog["/var/lib/election-pipeline-deployment"]
        CAT["catalog.json<br/>source → image, flags, publication"]
    end
    R1 -- "deploy.py run" --> CAT
    CAT -- "selects image" --> R2
    B1 --> B2
```

- Each expansion source is pinned to one immutable image; a code fix moves it with
  `deploy.py repin` after a validated prepare-only run.
- Expansion jobs run in containers with `--read-only`, `--cap-drop ALL`, a non-root
  user, PID limits and `EXPANSION_CPUS` / `EXPANSION_MEMORY` caps.
- Candidate dashboards start privately on `127.0.0.1:<port>`; `activate` switches
  Caddy to one and keeps the previous running for rollback.

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
    participant T as Backup timer (00/6:02 UTC)
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
