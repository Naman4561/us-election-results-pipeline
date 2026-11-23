# State status

Where each state's 2026 general election results stand. Updated by hand; last updated
**November 23, 2026, 15:10 UTC**. [States](states.md) describes each source.

| Status | Meaning |
| --- | --- |
| **Official** | The state certified its results; we publish them and no longer collect. |
| **Unofficial, counting** | The state is still posting new counts; we collect them as they appear. |
| **Unofficial, between counts** | The state counts on a published schedule; the next count is dated. |
| **Unofficial, pending certification** | Was live, but the state's count has stopped. Collected less often until the state certifies or posts more. |
| **Unofficial, paused** | Collection is stopped by an operator (`deploy pause`); the last publication stays public. |
| **Unofficial, imported by hand** | An operator saves the state's results page or export and imports it (`scripts/manual_source.py`); it changes when a newer copy is imported. |
| **Final unofficial** | The count is complete; nothing more is expected before certification. A hand-imported source is marked with `manual final`, a collected one is paused and marked with `deploy final`. |
| **Waiting for certification** | No usable results yet; the next step is the state's certified results. |
| **Not public yet** | Configured, but not on the public dashboard. |
| **Awaiting certified results** | No election-night source we collect; added from the state's certified canvass. |
| **Not covered** | No official source we can use. |

"Last vote change" is when we first saw a race total change, in UTC.

| State | Status | Last vote change | Notes |
| --- | --- | --- | --- |
| Alabama | Unofficial, pending certification | Nov 4 17:16 | Public since Nov 19, 3:45 PM EST, collected from its statewide page and Export Data workbook; every county adds up to the page's statewide votes. 67 of 67 counties reported; the page last updated Nov 4, 11:16 AM Central. |
| Alaska | Unofficial, between counts | Nov 19 01:33 | Results by State House district (overseas ballots are statewide only). No ranked-choice tabulation posted yet (checked Nov 19); certification targeted Nov 25. |
| Arizona | Unofficial, imported by hand | Nov 19 05:00 | The Secretary of State's Enhanced Voting export (JSON, about 72 MB), statewide and by county; generic write-ins combined. |
| Arkansas | Official | Nov 19 16:36 | Certified: its feed marks the results official (`isOfficial`, version of Nov 19, 11:36 AM EST, 23 votes more than Nov 16's). Published as official Nov 19 with `deploy certify`. Collection paused. |
| California | Unofficial, counting | Nov 18 01:47 | |
| Colorado | Unofficial, counting | Nov 13 23:50 | |
| Connecticut | Unofficial, counting | Nov 19 19:53 | Public since Nov 19, 3:45 PM EST, collected from its results app (version 55530); results by town (169); fusion party lines added up per candidate. 774 of 774 polling places. The app shows no update time of its own, so the time here is when we first saw these totals. |
| Delaware | Official | Nov 10 19:30 | Certified; collection paused. |
| Florida | Unofficial, counting | Nov 17 16:15 | Collection resumed: GoDaddy TLS Root CA - R1 is trusted for Florida's file host only. |
| Georgia | Official | Nov 19 23:02 | Certified: its feed marks the results official (`isOfficialResults`, version of Nov 20, 10:50 AM EST; totals unchanged from Nov 19). Results by county (159) since Nov 21, 9:48 PM EST, from its ballot items; they match the Secretary of State's Total Votes Excel. Published as official Nov 21, 9:50 PM EST with `deploy certify`. Collection paused. |
| Hawaii | Official | Nov 11 06:38 | Published as official Nov 17, 06:56 UTC; totals unchanged from Nov 11. Collection paused. |
| Idaho | Official | Nov 11 05:20 | Certified: Idaho's feed marks its results official (`isOfficialResults`, seen Nov 16). Published as official on Nov 16, 21:12 UTC with `deploy certify`; totals unchanged from Nov 11. Collection paused. |
| Indiana | Unofficial, counting | Nov 17 15:18 | Its settings carry a certified flag. |
| Iowa | Unofficial, pending certification | Nov 17 19:01 | Collected hourly. |
| Kansas | Official | Nov 9 02:31 | Published as official Nov 17, 16:05 UTC; totals unchanged from the hand import. |
| Kentucky | Unofficial, imported by hand | Nov 20 15:54 | Public since Nov 20, 1:30 PM EST. Its results pages saved by hand (the live feed and its data files refuse automated clients): Statewide page with each race's county results, Last Update Nov 20, 10:42 AM EST, 120 of 120 counties reporting; totals only. |
| Louisiana | Official | Nov 14 11:17 | Certified: its feed marks the Nov 3 results official (`ResultsOfficial`, seen Nov 19, 8:48 AM EST). Published as official Nov 19, 15:34 UTC with `deploy certify`; totals unchanged from Nov 14. Collection paused. House districts 5 and 6 go to a December 12 runoff, marked “Runoff pending” on the dashboard. |
| Maryland | Unofficial, counting | Nov 18 20:16 | |
| Michigan | Unofficial, imported by hand | Nov 19 04:40 | The Secretary of State's county results export (MVIC), report updated Nov 18, 7:17 PM EST, 83 of 83 counties. |
| Minnesota | Unofficial, counting | Nov 13 20:04 | The State Canvassing Board certifies Nov 19. |
| Mississippi | Final unofficial | Nov 19 04:32 | All 82 counties imported from their certified Official Recapitulations (reviewed Nov 17–19; Walthall's was posted as “Walthal County.pdf”). Collection paused Nov 19 (`deploy pause`, `deploy final`): every county had certified, and each run saved a new bundle differing only in its status time. House districts published Nov 18 (`deploy ms-house`); all four are complete. The state certifies Dec 1 or 3: import its statewide recap then ([Mississippi](mississippi-collection.md#certification)). |
| Missouri | Unofficial, imported by hand | Nov 19 05:00 | The Secretary of State's All Results page, saved as a PDF. Statewide and congressional district totals only (no county results); no Senate race in 2026. |
| Montana | Unofficial, counting | Nov 19 18:50 | Public since Nov 19, 3:45 PM EST, collected from its Statewide Export; the site's TEST banner is hidden now and its export answered every request. 726 of 728 precincts; site updated Nov 19, 11:50 AM Mountain. |
| Nebraska | Unofficial, pending certification | Nov 14 01:00 | All precincts in. Collected every 3 hours, clear of backups. |
| Nevada | Unofficial, imported by hand | Nov 13 23:36 | Public since Nov 19, 7:28 PM EST. Its results app's data files saved by hand (release 90, Nov 13, 23:36 UTC); every county adds up to the statewide totals; Lincoln's District 2 votes are masked by the state. |
| New Hampshire | Unofficial, imported by hand | Nov 19 21:01 | Public since Nov 19, 7:28 PM EST. Its four results spreadsheets imported by hand (its site refuses automated clients); every race by county. |
| New Jersey | Unofficial, imported by hand | Nov 20 19:08 | Public since Nov 19, 7:28 PM EST. Its 21 county clerks' results assembled into one zip (15 Clarity counties fetched; Bergen, Hunterdon and Warren saved by hand); rebuilt Nov 21, 8:34 PM EST for Hudson's update of Nov 20, 2:08 PM EST, the newest county update. |
| New Mexico | Unofficial, pending certification | Nov 12 19:01 | The State Board of Canvass certifies Nov 24. Collected hourly. |
| New York | Unofficial, imported by hand | Nov 19 05:15 | The State Board of Elections' election night page, saved as a PDF. Fusion party lines are added up per candidate. |
| North Carolina | Unofficial, counting | Nov 14 00:31 | |
| North Dakota | Official | Nov 17 21:46 | The State Canvassing Board certified Nov 18 and the feed marks its results official. Published as official Nov 18, 20:29 UTC with `deploy certify`; totals unchanged from Nov 17. Collection paused. |
| Ohio | Final unofficial | Nov 12 03:26 | Imported by hand; the Nov 11, 10:24 PM EST report is the last before certification (re-checked Nov 12, 10:16 AM EST); marked final with `manual final` from the next release. |
| Oklahoma | Official | Nov 13 23:24 | Certified: the State Election Board labels its Nov 3 results “OFFICIAL RESULTS” (state certification scheduled Nov 10). Its county results export, imported by hand, was published as official on Nov 13 with an operator attestation; totals unchanged from Nov 11. |
| Oregon | Unofficial, counting | Nov 18 20:18 | Its file host sometimes answers a county CSV with another file type; that run stops unpublished and a later one collects it. |
| Pennsylvania | Unofficial, imported by hand | Nov 19 17:32 | Public since Nov 19, 1:29 PM EST. The Department of State's county results export (its site and RSS feeds refuse automated clients; the feeds stopped updating Nov 10). Report time declared at import (the export's own, 12:32:52 PM EST). Election Day, mail and provisional votes. |
| Rhode Island | Unofficial, counting | Nov 18 01:18 | Statewide and district totals only (reported by town). |
| South Carolina | Official | Nov 11 19:48 | The State Election Commission certified on Nov 11. Its feed sets no final flag, so the published snapshot was checked against the certified results and approved (operator attestation, Nov 13); collection paused. |
| South Dakota | Unofficial, pending certification | Nov 6 20:56 | All precincts in. Collected every 3 hours, clear of backups. |
| Tennessee | Unofficial, imported by hand | Nov 19 05:15 | The Secretary of State's Followed Races page, saved as a PDF; its report time (Nov 19, 12:15 AM EST) is the operator's. |
| Texas | Unofficial, pending certification | Nov 4 22:21 | All 254 counties in; state's last update Nov 4, 22:48. Collected every 3 hours, clear of backups. |
| Utah | Unofficial, counting | Nov 18 19:01 | |
| Vermont | Official | Nov 10 17:54 | Certified Nov 12 16:35 from its feed's official marker, checked against the state's results; collection paused. Map and results by town. |
| Virginia | Unofficial, counting | Nov 18 15:17 | Public since Nov 12, 7:48 PM EST. Its results API’s CDN refuses the server’s IPv4 address (403) and answers its IPv6 one, so its source prefers IPv6 (`prefer_ipv6`, through `JOB_IPV6_NETWORK`). Map and results by locality. While provisional ballots are added, its statewide counts can trail its localities’ sum; such a file is refused and the last matching one stays public (it was statewide-only on Nov 12 for that reason: `county_results=False` in `states.py`). On Nov 13 its VA-03 provisional counts lagged this way several times, each clearing within an hour or two. |
| Washington | Unofficial, counting | Nov 18 20:19 | Counts on weekdays. A run that sees the file change mid-download stops unpublished; the next one collects it. |
| West Virginia | Unofficial, counting | Nov 10 16:01 | |
| Wyoming | Official | Nov 10 21:37 | Added from its certified results (certified Nov 10). |
| Maine | Official, imported by hand | Nov 23 | Its official tabulations (sent to the Governor Nov 23) and District 2 ranked-choice summary, saved by hand (its results page refuses the pipeline); every race by county, military and overseas ballots statewide only. District 2's ranked-choice tabulation elected LePage in the first round. Not yet released. |
| Massachusetts | Unofficial, counting | — | Public since Nov 23, 9:57 AM EST. Collected hourly from PD43+, which lists an election once certified: each race's results by city and town (351, town map). The CSVs carry no certification flag, so it stays unofficial until an operator certifies it. |
| Illinois | Unofficial, partial (27 of 102 counties) | Nov 21 18:58 | Public since Nov 23, 9:57 AM EST. Collected election authority by authority (the State Board posts results after its December canvass): 27 counties from 31 of 108 authorities (Clarity sites, Chicago's results, platinumelectionresults, pollresults.net, GBS, livevoterturnout, and signed reviews of Cook's, Kane's, Lawrence's, Grundy's, Stark's and Iroquois's reports). A county is published once all its authorities are in (Vermilion waits for Danville). Partial counts named in the update text: DuPage, McLean, Warren, Winnebago. |
| Wisconsin | Unofficial (72 of 72 counties) | Nov 22 01:31 | Public since Nov 20, 8:14 PM EST; all 72 counties since Nov 21, 8:42 PM EST. Collected county by county from each county's own results report (the Elections Commission posts results only once it certifies, about Dec 1): 44 counties canvassed, 28 unofficial; reports no reader takes are transcribed and signed by the operator. Adams, Sheboygan, Taylor and Outagamie are their election-night reports (Adams absentee 0 of 19), the best each has posted. Every House district is complete. |
