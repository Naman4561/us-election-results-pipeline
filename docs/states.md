# States

Twenty-four states, every 2026 U.S. Senate, Governor and House race each one holds,
taken only from the state's own election office. The registry
(`src/election_pipeline/states.py`) and `config/sources.toml` are the source of truth;
this page explains them.

| State | Source | Races | County results | Vote methods stored |
| --- | --- | --- | --- | --- |
| Alaska | Division of Elections precinct CSV | Senate, Governor, House at-large (3) | No | Total only (first choice) |
| Arkansas | Tally (Secretary of State) | Senate, Governor, House 1–4 (6) | All 75 counties | Total only |
| California | Secretary of State results API | Governor, House 1–52 (53) | No | Total only |
| Colorado | Clarity | Senate, Governor, House 1–8 (10) | All 64 counties | Total only |
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
| Minnesota | Secretary of State media files | Senate, Governor, House 1–8 (10) | All 87 counties | Total only |
| Mississippi | County election offices | Senate (1) | Covered counties | Total only |
| Nebraska | Secretary of State results CSV | Senate, Governor, House 1–3 (5) | No | Total only |
| New Mexico | ENR export | Senate, Governor, House 1–3 (5) | All 33 counties, small counts masked | Total only |
| North Carolina | State Board precinct ZIP | Senate, House 1–14 (15) | All 100 counties | Election Day, early, mail, provisional |
| North Dakota | Tally (Secretary of State) | House at-large (1) | All 53 counties | Total only |
| Ohio | Saved by hand from the results XML | Senate, Governor, House 1–15 (17) | All 88 counties | Total only |
| Oregon | ENR export | Senate, Governor, House 1–6 (8) | All 36 counties | Total only |
| South Carolina | Clarity (Election Commission) | Senate, Governor, House 1–7 (9) | All 46 counties | Total only |
| South Dakota | ENR export | Senate, Governor, House at-large (3) | All 66 counties | Total only |
| Texas | Secretary of State Civix API | Senate, Governor, House 1–38 (40) | No | Total only |
| Utah | Enhanced Voting | House 1–4 (4) | All counties of each district | Total only |
| Virginia | Enhanced Voting | Senate, House 1–11 (12) | All 133 localities | Early, Election Day, mail, provisional, post-election |
| Washington | Enhanced Voting | House 1–10 (10) | All 39 counties | Total only |
| West Virginia | Clarity | Senate, House 1–2 (3) | All 55 counties | Total only |

A state without a Senate or Governor race in 2026 has none listed. Ohio, Kansas and
Mississippi's reviewed counties are imported by hand; every other source is collected
every 15 minutes, or hourly or every three hours once its count stops
(`config/sources.toml`). [State status](state-status.md) records where each state's
count stands.

## What each source does not give

Where a state publishes something only partly, or in a form that cannot be checked,
the pipeline leaves it out rather than estimate it. A missing count is unknown, never
zero.

- **Alaska:** statewide totals for the candidates in the first-choice CSV (round 1).
  Ranked-choice rounds are not imported, and reporting units are not mapped to boroughs.
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
- **Ohio:** the official XML is saved by hand ([manual results](manual-results.md)).
- **South Carolina:** six vote methods are checked against each county's count, but
  only totals are stored. Snapshots from before Nov 9, 2026 hold the commission's
  election-night workbooks instead, which stopped at midnight on election night.
- **Texas:** statewide and district totals; no county detail in the feed. Candidate sums
  must equal each contest's reported total.

Every Tally document (Arkansas, North Dakota) repeats the publication's version, so a
bundle never pairs two publications. Louisiana's documents carry no shared stamp; its
parish counts must add up to the statewide totals, so a collection that straddles an
update fails and is retried. Its U.S. House races are open to every candidate, and one
without a majority goes to a December runoff; the dashboard shows the November 3 count.
Indiana's settings, Louisiana's election list and Tally's documents each carry the
state's own official flag, which the parser records.

## Awaiting certified results

These states hold elections this year but have no election-night source collected
here; the map labels them "Awaiting certified results", and each is added from its
certified canvass (`deploy certify --certified-file`): Alabama, Arizona, Connecticut,
Delaware, Illinois, Kentucky, Maine, Massachusetts, Michigan, Missouri, Montana, Nevada,
New Hampshire, New Jersey, New York, Oklahoma, Pennsylvania, Rhode Island, Tennessee,
Vermont, Virginia, Wisconsin and Wyoming (`awaiting_certified` in
`resources/elections_2026.json`). Arizona, Michigan, Missouri, New Jersey, New York,
Nevada, Oklahoma, Pennsylvania and Tennessee refuse automated clients on their results
sites.

## Not covered

- **Maine** reports by municipality on election night, with no statewide endpoint, and
  its federal races need a first-choice/ranked-choice distinction.
- **Montana**'s results pages carry a "TEST" banner and its export fails on about half of
  requests, so its figures cannot be validated as official returns.
