# Processing and modelling

After collection, one pipeline run per source takes a raw snapshot to a public
publication in three more stages: **parse**, **load**, then **validate and publish**.
Each stage is skipped when its input has not changed (`already_parsed`, `skipped`,
`already_current`).

## Stages of one run

```mermaid
flowchart LR
    RAW[("raw snapshot<br/>sha256")] --> PARSE
    subgraph PARSE["Parse · parsing.py + parsers/{state}.py"]
        P1["Read official format<br/>CSV, XML, XLSX, JSON, HTML"]
        P2["Check scope against roster<br/>races, candidates, counties"]
        P3["Reconcile county to state totals<br/>and vote methods"]
        P1 --> P2 --> P3
    end
    PARSE --> CSV[("parsed/<br/>contest_results.csv<br/>county_results.csv")]
    CSV --> LOAD["Load · database.py<br/>transactional COPY<br/>into {state}.results"]
    LOAD --> DBT
    subgraph DBT["dbt build · tag:state_{xx}"]
        STG["staging<br/>stg_{xx}_releases<br/>stg_{xx}_results"]
        MART["marts<br/>{xx}_contest_candidate_results<br/>{xx}_county_candidate_results"]
        TEST["data tests<br/>completeness · counts ·<br/>reconciliation · vote methods"]
        STG --> MART --> TEST
    end
    DBT --> PUB["Promote · publication.py<br/>copy into frozen tables<br/>under a publication_id"]
    PUB --> CUR["serving.current_publications<br/>switch in one transaction"]
```

A parser that finds an unexpected race, candidate or county, a county total that does
not match the state, or vote methods that do not add up, stops the run with
`validation_failed`; nothing is loaded.

## Database layout

```mermaid
flowchart TB
    subgraph ops["ops · lineage"]
        src["sources"]
        snap["snapshots"]
        fetch["fetch_attempts"]
        rel["processing_releases"]
        load["load_attempts"]
        pa["publication_attempts"]
    end
    subgraph state["one schema per state · nc ky mn … hi"]
        res["results<br/>loaded rows per release"]
        con["contest_candidate_results"]
        cty["county_candidate_results"]
    end
    subgraph analytics["dbt schemas"]
        stg["staging models"]
        mart["mart models"]
    end
    subgraph serving["serving · public read"]
        pubs["publications"]
        cur["current_publications"]
    end
    src --> snap --> rel --> load
    src --> fetch
    rel --> res --> stg --> mart
    rel --> pa --> pubs --> cur
    mart --> con & cty
    pubs --> con & cty
```

```mermaid
erDiagram
    SOURCES ||--o{ SNAPSHOTS : "archives"
    SOURCES ||--o{ FETCH_ATTEMPTS : "logs"
    SNAPSHOTS ||--o{ PROCESSING_RELEASES : "parsed into"
    PROCESSING_RELEASES ||--o{ LOAD_ATTEMPTS : "loaded by"
    PROCESSING_RELEASES ||--o{ STATE_RESULTS : "rows"
    PROCESSING_RELEASES ||--o{ PUBLICATION_ATTEMPTS : "validated by"
    PUBLICATION_ATTEMPTS ||--o| PUBLICATIONS : "promotes"
    PUBLICATIONS ||--o{ CONTEST_CANDIDATE_RESULTS : "freezes"
    PUBLICATIONS ||--o{ COUNTY_CANDIDATE_RESULTS : "freezes"
    PUBLICATIONS ||--o| CURRENT_PUBLICATIONS : "is live"
```

The dashboard role can read only `serving` and each state's two published tables
(`GRANT SELECT` in each migration); it cannot see `ops`, raw rows or dbt schemas.

## dbt layers

```mermaid
flowchart LR
    subgraph sources["dbt sources"]
        s1["ops.processing_releases<br/>ops.snapshots · ops.sources"]
        s2["{xx}.results"]
    end
    subgraph macros["macros/total_only_states.sql"]
        m1["total_only_releases"]
        m2["total_only_results"]
        m3["total_only_contests<br/>vote shares per release and race"]
        m4["total_only_counties"]
    end
    subgraph tests["data tests per state"]
        t1["release_completeness<br/>every configured race present"]
        t2["candidate_counts<br/>≥ 2 choices, no negatives"]
        t3["county_reconciliation<br/>counties = statewide<br/>(masked: never more)"]
        t4["vote_methods<br/>methods sum to total"]
        t5["unit tests<br/>shares computed per release"]
    end
    s1 --> m1
    s2 --> m2
    m1 & m2 --> m3 & m4
    m3 & m4 --> tests
```

Eighteen states use these shared macros, and their per-state SQL and YAML files are
generated from the registry by `scripts/new_state.py`. NC, KY, MN, NE, TX and MS keep
hand-written models. Each run selects only its state, `--select tag:state_<xx>`, so
states publish independently.

## Publication rules

```mermaid
stateDiagram-v2
    [*] --> Candidate: dbt build in an isolated schema
    Candidate --> Rejected: any node failed, warned or skipped
    Candidate --> Unchanged: outputs identical to live
    Candidate --> Rejected: release older than live, needs explicit restore
    Candidate --> Published: promote under new publication_id
    Published --> Live: current_publications switched
    Live --> [*]
    Unchanged --> [*]: already_current
    Rejected --> [*]: live publication stays
```

A per-state advisory lock stops two publications for one state from overlapping;
different states publish in parallel. Florida orders releases by the official file's
own timestamps, so an older file served again by the state cannot replace newer
results.
