# The election definition

Status: **steps 3.1 and 3.2 of the plan (R-008), built on the Phase 3 branch, not yet
released.** The definition and its loader (`election_pipeline.elections`) are tested against
a golden record of today's registry. The registry (`STATES`), `load_config` and every source
now read it; `config/sources.toml` is gone. TLS exceptions and each source's acquisition are
declared in it. Still to move are the rosters and the reviewed per-state tables (under
`resources/` for now). This page says what is per-election, where it lives, what the
definition holds, and how each step proves it changed nothing.

## Why

The 2026 election is baked into the code. Per-election facts (which races are up, each
state's source, vote methods, how a source is acquired) sit in `State` beside durable facts
(a state's name, its parser). 42 parsers refuse any election date but `2026-11-03`; 194
literals of "2026" are in the 50 state parsers alone. How a source is acquired is not in
the repository at all: 17 of the 50 are manual, and that is recorded only in production's
catalog. For another cycle, someone would edit code across the package, and the repository
could not say how any source was acquired. Moving the per-election facts into data fixes
both.

## What is per-election today (the inventory)

| Where | What | Count |
|---|---|---|
| `states.py` `State` | 17 fields per state (below) | 50 states |
| `config/sources.toml` | url, election date, format, expected members, intervals, limits | 51 sources (50 results, 1 NC demographics) |
| production catalog only | `acquisition` http / manual, `manual_final`, max age | 33 http, 17 manual (AZ IL KS KY ME MI MO NH NJ NV NY OH OK PA TN WI WY) |
| `resources/rosters_2026/` | reviewed candidates and counties | 48 states; AK and MS keep their own scope files |
| `resources/*_2026_*.json` | IL, NJ, WI contests; NH towns; MS counties and House; AK scope | 6 files |
| `resources/elections_2026.json` | which states elect a Senator or Governor, certification sources | 1 file, read by the dashboard |
| 42 parsers | `source.election_date != "2026-11-03"` refusals | 42 |
| shared readers | Clarity's 2026-11-03..12-31 window, "2026 General", "11/3/2026"; ENR's `/2026` regex; Enhanced Voting and Tally date checks | 5 modules |
| collectors | SC's upload-path regex and Indiana's version code | 2 |
| `publication.py` | `FLORIDA_SOURCE`, `IOWA_SOURCE`: release-ordering rules keyed on 2026 source IDs | 2 |
| `dashboard/server.py` | `RUNOFFS` (LA-05, LA-06 on 2026-12-12) | 1 |
| `recovery.py` | an advisory lock key that is the election date, 20261103 | 1 |
| `tls.py` | per-host exceptions: Alaska's issuer chain, Hinds County's CBC suites, Florida's root | 3 |
| `deployment/control.py` | which resource files each state owns, for release check keys | 1 |

How the 50 state parsers read: 27 have their own reader; the rest sit on a platform (Enhanced
Voting 7, Clarity 6, ENR 5, Tally 3, and workbook readers).

### The `State` fields, sorted

| Durable (kept in code) | Per-election (move to the definition) |
|---|---|
| `code`, `name`, `parser_module`, `local_unit` | `source_id` (merged with the source entry), `house`, `senate`, `governor`, `methods`, `county_results`, `source_page`, `vote_basis`, `collection_backend`, `county_feed_status`, `county_privacy_masking`, `method_note`, `statewide_only_votes`, `optional_house`, `collected_counties` |

`local_unit` stays durable because a state's kind of place does not change between cycles
(towns, counties, House districts); `county_privacy_masking` is per-election because a state
may stop masking.

## Layout

```
config/elections/2026-general/
  election.toml          # id, date, every collector limit, the TLS exceptions ([[tls]])
  states/CT.toml         # one file per state: races, methods, reporting policy, source,
                         # how the source is acquired
  datasets/<name>.toml   # a source that is not a state's results (NC's absentee file)
```

It is under `config/` because the image already copies that folder, beside
`sources.toml`, which the definition replaces in step 3.2. Rosters, county tables and the
reviewed per-state tables move beside it in the steps that need them (3.2, 3.5); they are
still under `resources/` now.

`states.py` keeps only what is true of a state in every election, as `elections.DURABLE`: its
name, its parser module and the kind of place it reports by. A state's file may not set
those. `elections.load()` joins the two into the `State` and `Source` objects the code
already uses.

### A state file

```toml
code = "CT"
methods = ["total_votes"]
collection_backend = "connecticut"   # only where a state differs from State's defaults

[races]                              # senate defaults true, governor false
senate = false
governor = true
house = [1, 2, 3, 4, 5]

[acquisition]
mode = "http"                        # http | manual (R-014)
# a manual source also has: final = true, max_age_seconds = 21600

[source]                             # the Source fields, named as in settings.Source
id = "ct_2026_general"
page = "https://ctemspublic.tgstg.net/#/home"   # landing page, never fetched
url = "https://ctemspublic.tgstg.net/ng-app/data/election/108/Version.json"
format = "bundle"
primary_filename = "version.json"     # always explicit (R-012)
expected_members = ["version.json", "lookup.json"]
min_interval_seconds = 600
```

A source's election date is the election's.

`election.toml` also declares each host that needs more than the platform's default TLS
(R-017): its exact hosts, what is added (`issuer_bundle`, `trusted_root` or `legacy_cbc`), the
packaged certificate and the SHA-256 its bytes must have, and why. The loader refuses a host
named twice, a certificate without its hash, or a non-forward-secret cipher suite;
`election_pipeline.tls` applies them with the same checks as before, and `tests/test_tls.py`
pins the reviewed set, so changing a trust decision means changing a test. The election file
and a state's own file are part of that state's release check key and publication hash
(`elections.fingerprint`): edit one and only the affected sources are re-checked.

Acquisition is read, not only recorded: `manual configure` takes a manual source's freshness
limit and final flag from its declaration and refuses a source the definition acquires over
HTTP; `deploy go-live` follows it; and the monitor reports `acquisition/<source>` if the
production catalog ever disagrees with it. The production catalog keeps what is runtime state
(enabled, public, status, publication ids); the definition holds what the repository must know
to reproduce it, including how each source arrives. County units stay a
`collection_backend` of an http source, as Mississippi's are today.

## How each step proves it changed nothing

1. **A golden snapshot, taken before the definition existed.** `tests/fixtures/registry/
   registry_2026.json` holds every `State` and `Source` field of today's code, the collector's
   limits and how each source is acquired (17 manual, with their final flags and limits, from
   production's catalog on Nov 25). `tests/test_elections.py` requires the definition to
   reproduce it exactly, part by part so a difference names its state or source, and, until
   3.2, to equal the objects `states.py` and `sources.toml` produce.
2. **3.1 changes no consumer.** It adds the definition, the loader and the tests only.
   Production code still reads `states.py` and `sources.toml`, so nothing can change.
3. **3.2 switches the consumers** (`STATES` and sources derive from the definition). Its
   proof is the whole suite, the golden test, and the nightly reproduction: all 50 live
   publications must re-create with identical parse output hashes, though the code hash
   differs.
4. **Parsers change last and one platform at a time** (3.3, 3.4): each parser's output files
   must hash the same before and after, shown on its fixture and by reproduction.

## Decisions (settled)

1. **TOML for definitions, JSON for rosters and tables**, the repository's existing split.
2. **Durable and per-election as sorted above**: `local_unit` is durable, and
   `county_privacy_masking` is per-election.
3. **Acquisition is declared in the definition** (`http` or `manual`); the catalog keeps the
   runtime flags. `deploy register` and `manual configure` read it (step 3.2).
4. **One election at a time.** The loader takes one election id. A runoff, special or primary
   beside the general is a later extension, designed with the contest IDs of step 3.6.
5. **Parsers receive the election as a parameter** (step 3.3), not from a module global, so a
   parser test can run against any election's definition.

## Not decided here

The county FIPS tables (3.5) and the vote-method vocabulary (3.6) need migrations and are
designed with their own steps. The release-order rule (3.7) changes live behavior and is
reviewed state by state before it is written.
