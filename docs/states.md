# States

Thirty-four states, every 2026 U.S. Senate, Governor and House race each one holds,
taken only from the state's own election office. The registry
(`src/election_pipeline/states.py`) and `config/sources.toml` are the source of truth;
this page explains them.

| State | Source | Races | County results | Vote methods stored |
| --- | --- | --- | --- | --- |
| Alaska | Division of Elections precinct CSV | Senate, Governor, House at-large (3) | By State House district (40) | Total only (first choice) |
| Arkansas | Tally (Secretary of State) | Senate, Governor, House 1–4 (6) | All 75 counties | Total only |
| Arizona | Saved by hand from the Secretary of State's Enhanced Voting export (JSON) | Governor, House 1–9 (10) | All 15 counties | Early ballots, Early A.R.S. 16-579, Late Early, Election Day, provisional (Arizona's own groups) |
| California | Secretary of State results API | Governor, House 1–52 (53) | No | Total only |
| Colorado | Clarity | Senate, Governor, House 1–8 (10) | All 64 counties | Total only |
| Delaware | Department of Elections results JSON | Senate, House at-large (2) | All 3 counties | Total only (methods checked) |
| Florida | Division of Elections results file | Senate, Governor, 27 House (29) | All 67 counties | Total only |
| Georgia | Secretary of State results API | Senate, Governor, House 1–14 (16) | No | Total only |
| Hawaii | Office of Elections `summary.txt` | Governor, House 1–2 (3) | No | Mail, in person |
| Iowa | Clarity | Senate, Governor, House 1–4 (6) | All 99 counties | Election Day, absentee |
| Idaho | Enhanced Voting | Senate, Governor, House 1–2 (4) | All 44 counties | Total only |
| Indiana | Election Division results JSON | House 1–9 (9) | All counties of each district | Total only |
| Kansas | Saved by hand from the results page | Senate, Governor, House 1–4 (6) | Reported counties | Total only |
| Kentucky | State Board live results | Senate, House 1–6 (7) | All 120 counties | Election Day, absentee |
| Louisiana | Secretary of State live results | Senate, House 1–6 (7) | All 64 parishes | Total only |
| Maryland | State Board results pages | Governor, House 1–8 (9) | All 24 jurisdictions | Total only |
| Michigan | Saved by hand from the Secretary of State's county results export | Senate, Governor, House 1–13 (15) | All 83 counties | Total only |
| Minnesota | Secretary of State media files | Senate, Governor, House 1–8 (10) | All 87 counties | Total only |
| Mississippi | County election offices | Senate (1) | Covered counties | Total only |
| Missouri | Saved by hand from the Secretary of State's All Results page (PDF) | House 1–8 (8) | No | Total only |
| Nebraska | Secretary of State results CSV | Senate, Governor, House 1–3 (5) | No | Total only |
| New Mexico | ENR export | Senate, Governor, House 1–3 (5) | All 33 counties, small counts masked | Total only |
| New York | Saved by hand from the State Board's election night page (PDF) | Governor, House 1–26 (27) | All 62 counties | Total only |
| North Carolina | State Board precinct ZIP | Senate, House 1–14 (15) | All 100 counties | Election Day, early, mail, provisional |
| North Dakota | Tally (Secretary of State) | House at-large (1) | All 53 counties | Total only |
| Ohio | Saved by hand from the results XML | Senate, Governor, House 1–15 (17) | All 88 counties | Total only |
| Oklahoma | Saved by hand from the State Election Board's county results export | Senate, Governor, House 1–5 (7) | All 77 counties | Election Day, early, mail |
| Oregon | ENR export | Senate, Governor, House 1–6 (8) | All 36 counties | Total only |
| Pennsylvania | Saved by hand from the Department of State's county results export (CSV text) | Governor, House 1–17 (18) | All 67 counties | Election Day, mail, provisional |
| Rhode Island | Enhanced Voting | Senate, Governor, House 1–2 (4) | No (reported by town) | Election Day, early, mail |
| South Carolina | Clarity (Election Commission) | Senate, Governor, House 1–7 (9) | All 46 counties | Total only |
| South Dakota | ENR export | Senate, Governor, House at-large (3) | All 66 counties | Total only |
| Tennessee | Saved by hand from the Secretary of State's Followed Races dashboard (PDF) | Senate, Governor, House 1–9 (11) | No | Total only |
| Texas | Secretary of State Civix API | Senate, Governor, House 1–38 (40) | No | Total only |
| Utah | Enhanced Voting | House 1–4 (4) | All counties of each district | Total only |
| Vermont | Secretary of State results JSON | Governor, House at-large (2) | All 247 voting towns (town map) | Total only |
| Virginia | Enhanced Voting | Senate, House 1–11 (12) | All 133 localities | Early, Election Day, mail, provisional, post-election |
| Washington | Enhanced Voting | House 1–10 (10) | All 39 counties | Total only |
| West Virginia | Clarity | Senate, House 1–2 (3) | All 55 counties | Total only |
| Wyoming | Secretary of State certified results (State Canvassing Board) | Senate, Governor, House at-large (3) | All 23 counties | Total only |

A state without a Senate or Governor race in 2026 has none listed. Ohio, Kansas,
Oklahoma and Mississippi's reviewed counties are imported by hand, and Wyoming is added
from its certified results alone (no election-night source); every other source is collected
every 15 minutes, or hourly or every three hours once its count stops
(`config/sources.toml`). [State status](state-status.md) records where each state's
count stands.

## What each source does not give

Where a state publishes something only partly, or in a form that cannot be checked,
the pipeline leaves it out rather than estimate it. A missing count is unknown, never
zero.

- **Alaska:** totals for the candidates in the first-choice CSV (round 1), statewide and
  by State House district (Alaska has no counties). Each precinct and each district's
  absentee, early and questioned row belongs to one district; overseas federal absentee
  ballots belong to none, so the districts add up to the statewide total less those.
  Ranked-choice rounds are not imported, and precincts are not mapped (about 45% of
  votes are reported by district only).
- **California:** statewide and district totals only. County results would take about
  190 more requests per collection, and the results website is behind a JavaScript
  challenge the pipeline does not attempt. The other 52 contest documents are fetched
  only when `governor.json` changes, or hourly.
- **Colorado, Oregon, Washington:** all-mail states that publish no method breakdown.
  Oregon's pages change on every request, so each collection repeats all 38 exports;
  its "0 of N precincts" while mail is counted is not stored. A candidate nominated by
  several parties keeps the first party for map colours.
- **Florida:** District 10 was uncontested and is not on the ballot. Statewide totals are
  the sum of the official county rows; the file has no report time, so releases are
  ordered by the file's own timestamps and an older file served again cannot replace
  newer results.
- **Georgia:** statewide and district totals from one request. Method sums are checked
  but not stored.
- **Hawaii:** county results are published only as PDFs, and the precinct file does not
  say which county a precinct is in, so county detail is not collected.
- **Iowa:** a county with no votes for a candidate prints Total 0 with Election Day and
  absentee blank; only then is a blank read as 0. Any other blank rejects the snapshot.
- **Kansas:** the results page is saved by hand ([manual results](manual-results.md)).
  Kansas stopped updating unofficial results after November 6, so the source is final.
  Counties that had not reported are grey on the map, never zero.
- **Kentucky:** absentee is combined and derived as total minus Election Day; separate
  early, mail and provisional counts are unavailable. Precincts reporting does not mean
  precincts complete.
- **Maryland:** methods are published, but provisional is "NR" until the canvass, so they
  are not stored yet. Each contest page has its own refresh time, all recorded.
- **Mississippi:** collected county by county, and only the Senate. Its statewide figure
  is the sum of the covered counties, not a statewide total; see
  [Mississippi collection](mississippi-collection.md).
- **Nebraska:** the official CSV has no county detail for these races.
- **New Mexico:** small county counts are printed as `*` for voter privacy. They are never
  reconstructed; a county sum may then fall short of the official total, never exceed it,
  and the map says "hidden for voter privacy".
- **North Carolina:** county totals are derived from precincts. A NUL party marker on
  write-in rows is stored as no party; no party is inferred. North Carolina's absentee
  demographics are a separate dataset in their own tables.
- **Arizona:** the Enhanced Voting export (JSON, about 72 MB with precinct detail) is
  saved by hand. Every county adds up to the statewide counts; county party labels vary,
  so the statewide party is used. Its five vote groups are stored under Arizona's own
  names (`early_ballot`, `early_ars_16_579`, `late_early`, `election_day`,
  `provisional`; migration 028): its "Early Voting" group is its early ballots, most
  cast by mail, so it is not stored as in-person `early_voting`. Each count's groups add
  up to it, and the counties' groups to the statewide ones. Its `createdAt` is the
  report time.
- **Pennsylvania:** the county results export (`UnOfficial_<time>.txt`, comma-separated) is
  saved by hand: the results site and its RSS feeds refuse automated clients, and the
  feeds stopped updating on November 10. It has no statewide rows (statewide totals
  are the county sums), no write-ins and no report time (declared at import). Each
  row's Election Day, mail and provisional votes must add up to its count.
- **Michigan:** the county results export (tab-separated text) is saved by hand (the
  results site refuses automated clients). It has no statewide rows: statewide totals are
  the county sums, with each office's write-ins as one line. It prints no report time,
  so the operator declares the site's "Updated" time at import.
- **Missouri:** the All Results page is saved as a PDF by hand (the results site refuses
  automated clients). It gives statewide district totals only; each district's
  candidates must add up to its printed Total Votes. Write-ins are one line.
- **Tennessee:** the Followed Races dashboard is saved as a PDF by hand. It gives
  statewide totals with percentages but no race totals: one total must fit every printed
  percentage (within 0.01) and be at least the candidates' sum. It prints no report time;
  the operator declares one.
- **New York:** the State Board's election night page is saved as a PDF by hand and read
  in layout mode. Candidates run on several party lines (fusion): each candidate's lines
  must add up to its printed total, each area's candidates, blanks, voids and write-ins to
  its Total Votes, and the counties to All Counties. Candidates are stored under their
  first party line; blanks and voids are not stored.
- **Ohio:** the official XML is saved by hand ([manual results](manual-results.md)).
- **Oklahoma:** the county results export is saved by hand (the results site refuses
  automated clients). It has no statewide rows: statewide totals are the county sums,
  and its report time is the date the site gives the export's one file when building it.
- **South Carolina:** six vote methods are checked against each county's count, but
  only totals are stored. Snapshots from before Nov 9, 2026 hold the commission's
  election-night workbooks instead, which stopped at midnight on election night.
- **Texas:** statewide and district totals; no county detail in the feed. Candidate sums
  must equal each contest's reported total.

Every Tally document (Arkansas, North Dakota) repeats the publication's version, so a
bundle never pairs two publications. Louisiana's documents carry no shared stamp; its
parish counts must add up to the statewide totals, so a collection that straddles an
update fails and is retried. Its U.S. House races are open to every candidate, and one
without a majority goes to a December runoff; the dashboard shows the November 3 count
and marks the 5th and 6th districts “Runoff pending · December 12” under their header
and on the map (`RUNOFFS` in `dashboard/server.py`).
Indiana's settings, Louisiana's election list and Tally's documents each carry the
state's own official flag, which the parser records.

Rhode Island reports by city and town plus a "Federal" locality for federal-only
ballots, which belongs to no county; its towns must add up to the statewide totals,
which are kept with no county map. Vermont reports by town, so its map draws towns
(Census county subdivisions) and its towns are stored where other states store
counties; each office's "STATE WIDE" entry must equal the sum of its towns. Delaware's
New Castle County is Wilmington plus the rest of the county; both parts are checked.

## Awaiting certified results

These states hold elections this year but have no election-night source collected
here; the map labels them "Awaiting certified results" until each is added from its
certified canvass, as a manual source whose first import is the certified file
([manual results](manual-results.md#a-state-added-from-its-certified-results)). Wyoming,
certified on November 10, is the first. The list: Alabama, Arizona, Connecticut,
Illinois, Kentucky, Maine, Massachusetts, Michigan, Missouri, Montana, Nevada, New
Hampshire, New Jersey, New York, Oklahoma, Pennsylvania, Tennessee, Virginia, Wisconsin
and Wyoming (`awaiting_certified` in
`resources/elections_2026.json`). Arizona, Michigan, Missouri, New Jersey, New York,
Nevada, Oklahoma, Pennsylvania and Tennessee refuse automated clients on their results
sites.

## Not covered

- **Maine** reports by municipality on election night, with no statewide endpoint, and
  its federal races need a first-choice/ranked-choice distinction.
- **Montana**'s results pages carry a "TEST" banner and its export fails on about half of
  requests, so its figures cannot be validated as official returns.
