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
        P1["Read official format<br/>CSV, XML, XLSX, JSON, HTML, PDF"]
        P2["Check scope against roster<br/>races, candidates, counties"]
        P3["parsers/results.py<br/>shared layout, derived totals,<br/>reconciliation, vote methods"]
        P1 --> P2 --> P3
    end
    PARSE --> CSV[("parsed/<br/>results.csv<br/>county_results.csv<br/>contest_results.csv")]
    CSV --> LOAD["Load · database.py<br/>transactional COPY into<br/>landing.results (+ methods)"]
    LOAD --> DBT
    subgraph DBT["dbt build · tag:results, the state's contract"]
        STG["staging<br/>stg_releases · stg_results<br/>stg_county_rows"]
        MART["marts<br/>contest_results · county_results<br/>*_method_results"]
        TEST["data tests<br/>completeness · counts ·<br/>reconciliation · vote methods"]
        STG --> MART --> TEST
    end
    DBT --> PUB["Promote · publication.py<br/>copy into serving tables<br/>under a publication_id"]
    PUB --> CUR["serving.current_publications<br/>switch in one transaction"]
```

A parser emits the rows its source publishes, statewide, county or precinct. The
shared `finish` derives what the source does not print: county totals from precincts
(North Carolina) and statewide totals from counties (Mississippi, Florida). A parser
that finds an unexpected race, candidate or county, a county total that does not match
the state, or vote methods that do not add up, stops the run with `validation_failed`;
nothing is loaded.

## Database layout

Every state shares one set of tables (migration `024_shared_results.sql`); each row
carries its state. Mississippi's county collection health and North Carolina's
absentee demographics keep tables of their own.

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
    subgraph landing["landing · parsed rows"]
        res["results<br/>state · county · precinct rows"]
        meth["result_methods<br/>votes per method"]
        units["reporting_units<br/>turnout per unit"]
    end
    subgraph analytics["dbt candidate schemas"]
        stg["staging models"]
        mart["mart models"]
    end
    subgraph serving["serving · public read"]
        pubs["publications"]
        cur["current_publications"]
        con["contest_results · county_results<br/>+ *_method_results"]
    end
    src --> snap --> rel --> load
    src --> fetch
    rel --> res --> stg --> mart
    res --> meth
    rel --> pa --> pubs --> cur
    mart --> con
    pubs --> con
```

```mermaid
erDiagram
    SOURCES ||--o{ SNAPSHOTS : "archives"
    SOURCES ||--o{ FETCH_ATTEMPTS : "logs"
    SNAPSHOTS ||--o{ PROCESSING_RELEASES : "parsed into"
    PROCESSING_RELEASES ||--o{ LOAD_ATTEMPTS : "loaded by"
    PROCESSING_RELEASES ||--o{ RESULTS : "rows"
    RESULTS ||--o{ RESULT_METHODS : "split by method"
    PROCESSING_RELEASES ||--o{ PUBLICATION_ATTEMPTS : "validated by"
    PUBLICATION_ATTEMPTS ||--o| PUBLICATIONS : "promotes"
    PUBLICATIONS ||--o{ CONTEST_RESULTS : "freezes"
    PUBLICATIONS ||--o{ COUNTY_RESULTS : "freezes"
    PUBLICATIONS ||--o| CURRENT_PUBLICATIONS : "is live"
```

The dashboard role can read only `serving` (and Mississippi's published county
status); it cannot see `ops`, `landing` or dbt schemas. The per-state schemas of
migrations 003–023 hold the rows from before consolidation; cutover copied them into
the shared tables (`consolidate.py`), and nothing writes to them any more.

## dbt

One project serves every state. `publication.build_candidate` builds one release and
passes its state's contract from the registry: `state`, `races`, `methods` and
`county_reconciliation` (`exact`, `masked` for New Mexico's privacy-masked counties,
or `none` for states without county results).

```mermaid
flowchart LR
    subgraph models["models/results (tag results)"]
        s["stg_releases · stg_results<br/>stg_result_methods · stg_county_rows"]
        m["contest_results · county_results<br/>contest/county_method_results"]
        s --> m
    end
    subgraph tests["tests (tag results)"]
        t1["release_completeness<br/>every race, every row, right state"]
        t2["candidate_counts<br/>all known or all unknown"]
        t3["county_reconciliation<br/>exact · masked · none"]
        t4["vote_methods<br/>the state's methods, summing to total"]
        t5["unit tests<br/>derivation rules"]
    end
    m --> tests
    ms["models/ms (tag ms)<br/>county feed status + test"]
    demo["models/demographics<br/>NC absentee demographics"]
```

Mississippi builds `tag:results tag:ms`; the demographics dataset builds
`tag:demographics`. Tests run with cautious indirect selection, so a test runs only
when every model it reads was built.

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
results; Iowa treats byte-different Clarity bundles of one results version as the
same release.
