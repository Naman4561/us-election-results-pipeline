# States

Thirty-four states, every 2026 U.S. Senate, Governor and House race each one holds,
taken only from the state's own election office. The registry
(`src/election_pipeline/states.py`) and `config/sources.toml` are the source of truth;
this page explains them.

| State | Source | Races | County results | Vote methods stored |
| --- | --- | --- | --- | --- |
| Alabama | Secretary of State's statewide page and its Export Data workbook (collected) | Senate, Governor, House 1–7 (9) | All 67 counties | Total only |
| Alaska | Division of Elections precinct CSV | Senate, Governor, House at-large (3) | By State House district (40) | Total only (first choice) |
| Arkansas | Tally (Secretary of State) | Senate, Governor, House 1–4 (6) | All 75 counties | Total only |
| Arizona | Saved by hand from the Secretary of State's Enhanced Voting export (JSON) | Governor, House 1–9 (10) | All 15 counties | Early ballots, Early A.R.S. 16-579, Late Early, Election Day, provisional (Arizona's own groups) |
| California | Secretary of State results API | Governor, House 1–52 (53) | No | Total only |
| Colorado | Clarity | Senate, Governor, House 1–8 (10) | All 64 counties | Total only |
| Connecticut | Secretary of the State's results app data files (collected) | Governor, House 1–5 (6) | No (reported by town; 169 towns) | Total only |
| Delaware | Department of Elections results JSON | Senate, House at-large (2) | All 3 counties | Total only (methods checked) |
| Florida | Division of Elections results file | Senate, Governor, 27 House (29) | All 67 counties | Total only |
| Georgia | Enhanced Voting (Secretary of State) | Senate, Governor, House 1–14 (16) | All 159 counties | Total only |
| Hawaii | Office of Elections `summary.txt` | Governor, House 1–2 (3) | No | Mail, in person |
| Iowa | Clarity | Senate, Governor, House 1–4 (6) | All 99 counties | Election Day, absentee |
| Idaho | Enhanced Voting | Senate, Governor, House 1–2 (4) | All 44 counties | Total only |
| Illinois | Each election authority's own results (Clarity sites, or transcribed by hand) | Senate, Governor, House 1–17 (19) | Collected counties | Total only |
| Indiana | Election Division results JSON | House 1–9 (9) | All counties of each district | Total only |
| Kansas | Saved by hand from the results page | Senate, Governor, House 1–4 (6) | Reported counties | Total only |
| Kentucky | State Board results pages, saved by hand | Senate, House 1–6 (7) | All 120 counties | Total only |
| Louisiana | Secretary of State live results | Senate, House 1–6 (7) | All 64 parishes | Total only |
| Maryland | State Board results pages | Governor, House 1–8 (9) | All 24 jurisdictions | Total only |
| Massachusetts | PD43+ (Secretary of the Commonwealth), one CSV per race | Senate, Governor, House 1–9 (11) | All 351 cities and towns (town map) | Total only |
| Michigan | Saved by hand from the Secretary of State's county results export | Senate, Governor, House 1–13 (15) | All 83 counties | Total only |
| Minnesota | Secretary of State media files | Senate, Governor, House 1–8 (10) | All 87 counties | Total only |
| Mississippi | County election offices | Senate (1) | Covered counties | Total only |
| Missouri | Saved by hand from the Secretary of State's All Results page (PDF) | House 1–8 (8) | No | Total only |
| Montana | Secretary of State's Statewide Export (collected) | Senate, House 1–2 (3) | All 56 counties | Total only |
| Nebraska | Secretary of State results CSV | Senate, Governor, House 1–3 (5) | No | Total only |
| Nevada | Saved by hand: the results app's manifest, overview and contest files, imported as one zip | Governor, House 1–4 (5) | All 17 counties | Total only |
| Maine | Saved by hand: the Secretary of State's official tabulations and District 2 ranked-choice summary, imported as one zip | Senate, Governor, House 1–2 (4) | All 16 counties; overseas ballots statewide only | Total only |
| New Hampshire | Saved by hand: the Secretary of State's four results spreadsheets, imported as one zip | Senate, Governor, House 1–2 (4) | All 10 counties (House from its towns) | Total only |
| New Jersey | Its 21 county clerks' results, assembled into one zip by `scripts/nj_bundle.py` | Senate, House 1–12 (13) | All 21 counties | Total only |
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
| Wisconsin | Each county's own results report, saved or transcribed by hand | Governor, House 1–8 (9) | Collected counties | Total only |
| Wyoming | Secretary of State certified results (State Canvassing Board) | Senate, Governor, House at-large (3) | All 23 counties | Total only |

A state without a Senate or Governor race in 2026 has none listed. Seventeen states'
files are imported by hand ([manual results](manual-results.md) lists them), as are
Mississippi's reviewed counties, and Wyoming is added from its certified results alone
(no election-night source); every other source is collected
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
- **Georgia:** the election's data document and one ballot item per race, each with its
  county breakdown; every county adds up to the statewide counts. Vote methods are not
  stored: Georgia leaves them blank in some counties (Ben Hill, Miller, Lumpkin and
  Glascock in its official results). Its Reports page's Total Votes Excel (County
  Results sheet) matched these county totals exactly on Nov 21.
- **Hawaii:** county results are published only as PDFs, and the precinct file does not
  say which county a precinct is in, so county detail is not collected.
- **Iowa:** a county with no votes for a candidate prints Total 0 with Election Day and
  absentee blank; only then is a blank read as 0. Any other blank rejects the snapshot.
- **Kansas:** the results page is saved by hand ([manual results](manual-results.md)).
  Kansas stopped updating unofficial results after November 6, so the source is final.
  Counties that had not reported are grey on the map, never zero.
- **Kentucky:** the live feed (with Election Day and combined absentee) refuses
  automated clients since election week, and its data files refuse a browser too. The
  operator saves the Statewide results page with each race's County Results view open
  (seven single-file pages, MHTML), imported as one zip (`parsers/ky_pages.py`). Each
  race's counties must add up to the page's statewide totals and every page must be the
  same update; parties are the feed's own party IDs. The pages give totals only.
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
- **Alabama:** the statewide results page and its "Export Data" workbook
  (`collectors/alabama.py`, posting the page's results form, not its search forms).
  Every county row adds up, candidate by candidate, to the page's statewide votes, and
  those to its contest total. Write-ins are one line; each county's precincts come from
  the workbook's Statistics sheet. Identical re-downloads (a new zip, same sheets) keep
  the previous bundle.
- **Connecticut:** the results app's static files: `Version.json`, then that version's
  lookup, statewide, town and status files (`collectors/connecticut.py`; a version
  change mid-collection fails the attempt). Candidates on several party lines (fusion)
  are added up under their major party; write-ins are one line. Towns must add up to
  statewide line by line. The map draws its 169 towns. The app prints no update time of
  its own, so the version number is recorded.
- **Massachusetts:** PD43+ (`electionstats.state.ma.us`) lists an election once its
  results are certified; each race's city/town CSV is fetched by its PD43+ election ID
  (configured in `sources.toml`). Every town's votes, write-ins ("All Others") and
  blanks must add up to its votes cast, and the towns to the TOTALS row; blanks are not
  stored. PD43+ abbreviates directions ("N. Adams"), written out as the map writes them;
  the map drops the Census "Town" suffix of towns with a city government ("Agawam").
  The CSVs carry no certification flag, so Massachusetts is published unofficial until
  an operator certifies it with an attestation.
- **Montana:** the site is South Dakota's platform without its statewide CSV. The
  collector posts the export page's "Statewide Export" button (`collectors/montana.py`);
  each sheet's county rows must add up to its TOTALS row. Every download embeds its own
  time, so the collector compares the worksheets without it and keeps the previous
  bundle while they match (refreshed hourly). Its "TEST" banner, shown on Nov 6, is in
  the page but hidden since. No write-ins or vote methods are reported.
- **Nevada:** the results site and its data files refuse automated clients. The operator
  saves the app's `manifest.json`, the overview it names and the Governor and House
  contest files it names; each must have the manifest's SHA-256. Every county adds up,
  choice by choice, to the overview's statewide totals. A county the state masks for
  privacy (Lincoln in District 2) is in neither and is left out of that contest. "None
  of These Candidates" is kept as a choice. The manifest's generation time is the
  report time.
- **New Jersey:** no statewide results before certification, so each county clerk's
  own results are assembled into one zip (`scripts/nj_bundle.py`, a `sources.json` with
  every member's address and SHA-256): 15 Clarity counties' summaries (fetched as the
  pipeline fetches any page), Camden's, Hunterdon's, Sussex's and Warren's ES&S summary
  reports, Salem's results page and Bergen's printed summary (Bergen, Hunterdon and
  Warren saved by hand). `resources/nj_2026_contests.json` maps each county's contest
  titles to races and lists every ballot candidate with its spellings; minor-party
  labels are ballot slogans and stand as IND. Every ballot candidate must be found in
  every county of its race; any other line is a write-in. Each county is checked on its
  own terms (Clarity totals, ES&S Total Votes Cast or vote methods, Bergen's
  percentages, Salem's precincts); statewide totals are the county sums. Refresh:
  rerun the bundle tool and import again.
- **Illinois:** the State Board posts results only after its December canvass, so each
  of the 108 election authorities' own results is collected as Wisconsin's counties are
  (`parsers/il.py`): the 102 counties' clerks or election commission, and six city
  election commissions (Bloomington, Chicago, Danville, East St. Louis, Galesburg,
  Rockford) inside their counties. One zip holds each collected authority's files and a
  `manifest.json`; a Clarity site's summary and settings are read as they are (each
  contest's candidates add up to its total, and its precincts reporting are kept), and
  anything else is transcribed into a signed review. A county is published once every
  authority in it is collected, as their sum. A count still under way (McLean's clerk, 14
  of 81 precincts in November 2026) is named in the publication's update text as a
  partial count. Each authority's own contest titles, and the candidates' spellings, are
  in the reviewed table `resources/il_2026_contests.json`; a name not in it is refused
  rather than counted as a write-in. A feasibility survey of all 108 (Nov 23) found
  Clarity sites, vendor sites (pollresults.net, GBS, platinumelectionresults,
  livevoterturnout), county PDFs and pages, and eight that refuse the pipeline. Each
  import is built by `scripts/il_bundle.py` from the previous one and an additions file
  naming the authorities added or replaced; it also writes and signs the reviews and
  parses a zip offline before the import. Refresh: pack, check, import.
- **Wisconsin:** the Elections Commission posts results only once it certifies (about
  December 1) and its site refuses automated clients, so each county's own report is
  collected as the counties post them (`parsers/wi.py`): fetched as the pipeline fetches
  any page where the county answers, saved by the operator where it does not. One zip
  holds every collected county's report unchanged and a `manifest.json` naming each
  report's file and SHA-256, address, save time, report time, whether it is the
  county's canvass or its election-night results, and how it is read. Readers take the
  layouts that recur (Election Summary Report, ES&S Summary Results Report, the canvass
  summary statement, Chippewa's summary, Oneida's results page), each checked against
  the totals it prints; any other report (a scan, a one-off layout) is transcribed into
  a review for its SHA-256, which the operator checks and signs. A county that posts its
  races as separate reports (Vilas) lists them as parts, read only through a signed review
  that names every part's SHA-256 in order. Each county must report
  the Governor and exactly the House districts the roster places it in (the Census
  county–district relationship file); `resources/wi_2026_contests.json` lists every
  ballot candidate (the Commission's candidate list) and the spellings counties print.
  Statewide votes are the collected counties' sums: the dashboard shows "N of 72
  counties collected" and no statewide lead until every county is in, and a House
  district is complete once all its counties are (`State.collected_counties`).
- **Maine:** the Secretary of State's results page refuses automated clients (403). The
  operator saves its official tabulations (Senate, Governor and District 1 by
  municipality, District 2 by precinct) and RCTab's District 2 ranked-choice summary and
  imports them, unchanged, as one zip. Every place's votes and blanks must add up to its
  ballots cast, every county's places to its total row, and the county totals with the
  military and overseas (UOCAVA) row to the sheet's total; those totals are formulas
  read from their stored values, so this checks the file is unedited. UOCAVA ballots are
  in no county, so they count statewide only (`statewide_only_votes`). The House counts
  are the ballots as marked; the ranked-choice summary must name the same ballots and a
  winner with a first-round majority (District 2, Nov 3, 2026), and is not stored. No
  report time is printed: it is declared at import.
- **New Hampshire:** the results site refuses automated clients (Akamai). The operator
  downloads its Senate and Governor county summaries and its two congressional district
  sheets (by town and city ward) and imports them, unchanged, as one zip. Each sheet's
  rows must add up to its TOTALS row, but those are Excel `SUM` formulas over the same
  rows (read from their stored values), so this checks the file is unedited rather than
  checking it against a second source. House towns are added up into their counties
  (`resources/nh_2026_towns.json`, from Census; a ward counts for its city). No report
  time is printed: it is declared at import.
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

The map labels a state "Awaiting certified results" while it holds elections this year
but has no public source (`awaiting_certified` in `resources/elections_2026.json`; a
state leaves the label once a source of its is public). Such a state is added from its
certified canvass, as a manual source whose first import is the certified file
([manual results](manual-results.md#a-state-added-from-its-certified-results)), or from
any results file it posts. As of November 23, 2026 every state is covered (Wisconsin and
Illinois are collected county by county, Massachusetts from PD43+, and Maine from its
official tabulations saved by hand, above). Illinois's State Board vote totals
(`elections.il.gov`) offered the 2026 primary as the newest election on Nov 23; once they
list the general, they can replace the collected counties.
