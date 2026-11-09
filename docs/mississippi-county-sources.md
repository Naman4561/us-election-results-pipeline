# Mississippi county source inventory

Original discovery checkpoint: 2026-11-05T02:20:32.633737+00:00. Election: November 3, 2026. Priority: U.S. Senate.

All 82 counties have a contact/directory entry and a bounded discovery pass. Thirteen current official county result sources were verified. The other 69 remain unresolved; a failed probe or absent search result does not prove that a county has published nothing. All 82 now have an intake slot; 13 have configured report URLs and 69 await official reports. This inventory does not attest to production activation.

The machine-readable inventory is [ms-2026-county-sources.json](source-research/ms-2026-county-sources.json). It records URLs, checked times, probe outcomes, current report formats, SHA-256 hashes, exact Senate row examples, and the next action for every county. The alphabetical Mississippi SOS county codes are not Census FIPS codes.

## Verified current reports

| County | Source | Format | Report time, local | Reporting precincts | Key qualification |
|---|---|---|---|---|---|
| DeSoto | [Official report](https://www.desotocountyms.gov/DocumentCenter/View/11878/Results-Summary) | pdf_text_electionware | 2026-11-03 22:24:00 | 47/47 | County totals only. The report does not identify which outstanding ballot categories remain. |
| Hancock | [Official report](https://www.hancockcounty.ms.gov/DocumentCenter/View/1224/Unofficial-Results) | pdf_text_electionware | 2026-11-03 21:31:00 | 26/26 | The county notice links folder 164. Its public frontend listing contains 28 documents, including the county summary and precinct files. Two files share the Lakeshore label; do not sum all folder documents. Precincts Complete is 0 of 26 although Precincts Reporting is 26 of 26. |
| Hinds | [Official report](https://www.co.hinds.ms.us/pgs/results/Unofficial%20Summary%20Report.pdf) | pdf_text_electionware | 2026-11-03 23:00:00 | 108/108 | The newer county site's indexed results page points to the legacy county host. The legacy landing heading says Primary Election; the linked PDF itself explicitly identifies the 2026 General Election. Require PDF identity validation on each changed capture. |
| Lauderdale | [Official report](https://www.lauderdalecounty.org/wp-content/uploads/2026/11/2026-election-with-absentee.pdf) | pdf_scanned_electionware | 2026-11-04 10:06:00 | 37/37 | Scanned PDF: source image reviewed. The county article says in-person and mail absentee votes are included, affidavit votes are not. Its generic /election-results/ link still opens a 2019 runoff and must not be used for 2026. |
| Madison | [Official report](https://www.madison-co.com/elections/election_results.pdf?20261104192738=) | pdf_scanned_electionware | 2026-11-04 14:33:00 | 46/46 | Scanned PDF: source image reviewed. The outage landing page still supplies a working results link. The page says updated November 4 at 3:48 PM; the captured PDF says 2:33 PM. Resolve future links from the county page because its cache query changes. Do not equate page and report times. |
| Monroe | [Official report](https://www.monroems.com/circuitclerk/results/CUMMULATIVE_RESULTS_ALL_PRECINCTS.pdf) | pdf_text_electionware | 2026-11-04 00:10:00 | 27/27 | The source explicitly excludes absentee and affidavit ballots. The large cumulative-results button points to an ALL_PRECINCTS.pdf URL that returns 404; the directory listing links the working CUMMULATIVE_RESULTS_ALL_PRECINCTS.pdf file. Do not add the summary to its constituent precinct reports. |
| Neshoba | [Official report](https://neshobacounty.net/news/general-election-results-from-november-3-2026/) | html_county_news | 2026-11-03 | Not supplied | The official county home page links this post, which calls the contested-race results complete but unofficial. No precinct denominator, write-in total, or ballot-method coverage is supplied. Those values remain unknown. |
| Pike | [Official report](https://www.pikecountyelectionresults.org/2026_general/DW.pdf) | pdf_text_electionware | 2026-11-03 21:39:00 | 25/25 | The county government site links its dedicated election-results domain. The district-wide report explicitly includes absentee votes. Preserve its 25-precinct contract; do not add precinct PDFs to this aggregate. |
| Rankin | [Official report](https://www2.rankincounty.org/results/index.html) | html_hart_cumulative | 2026-11-03 22:18:19 | 46/46 | The official archive links the HTML result host and a November 2026 cumulative PDF. The PDF reports the same Senate candidate totals, with a generation time 94 seconds later. This is agreement between renderings, not independent evidence of all ballots counted. Write-ins are not supplied; no residual should be assigned to a candidate. |
| Warren | [Official report](https://warrencountyms.gov/wp-content/uploads/2026/11/General-Unofficial-November-3-Election-Results.pdf) | pdf_scanned_votingworks | 2026-11-04 10:12:44 | Not supplied | Scanned PDF: source image reviewed. The 16 votes are explicitly Unadjudicated Write-In. Keep that label and adjudication status. OCR/text extraction misses several House and judicial values; it must not be accepted without image review. No precinct denominator is supplied. |

The [captured reports archive](../tests/fixtures/mississippi/reports.zip) preserves the ten original county captures plus Rankin’s alternate PDF and a provenance manifest. SHA-256 values are recorded in the JSON inventory. These fixed captures support offline parser development; they are not a live statewide dataset.

## Collection

How the configured counties are collected, validated and imported by review is in
[Mississippi collection](mississippi-collection.md). This page is the research inventory
behind it.

## Remaining source gaps

High-priority follow-up: Harrison, Jackson, Lee, Forrest, Lowndes, Lafayette, Jones, Lamar, and Pearl River. Their absence can materially distort an aggregate. Other county gaps remain equally required for full statewide collection.

Pearl River has a current county alert linking an ArcGIS application, but the referenced configuration names older primary/local races and its election results service did not supply a verified Senate dataset. Tippah has an indexed official results page, but returned 502/503 here. Those are leads, not accepted feeds. Bolivar/Jackson/Leake/Lafayette point to older elections or old social-media announcements. Several directory referral URLs are outdated, temporary placeholders, or nongovernment directory listings.

The SOS county directory supplies a circuit-clerk contact route for unresolved counties. Request an official November 3, 2026 cumulative Senate report or CSV, generation time, included and outstanding ballot categories, and the URL or delivery method for later revisions. No requests have been sent. Do not substitute news totals for missing official county captures.

## All-county register

`verified_current_source` means a current original source was inspected, not that a parser or publication passed acceptance. Other statuses document the unresolved lead or observed limitation.

| SOS county code | County | Discovery status | Follow-up source |
|---|---|---|---|
| 01 | Adams | current_report_not_located | [Source / lead](https://www.adamscountyms.net/county-departments/elections/) |
| 02 | Alcorn | current_report_not_located | [Source / lead](https://www.alcorncounty.org/circuit-clerk/) |
| 03 | Amite | current_report_not_located | [Source / lead](https://www.amitecounty.ms/election-commissioners) |
| 04 | Attala | current_report_not_located | [Source / lead](https://www.attalacounty.net/election-commission) |
| 05 | Benton | landing_only | [Source / lead](https://www.bentoncountyms.gov/tools-and-tips) |
| 06 | Bolivar | older_results_only | [Source / lead](https://bolivar-circuit-clerk.ms/election-results/) |
| 07 | Calhoun | current_report_not_located | [Source / lead](http://www.calhouncoms.com/) |
| 08 | Carroll | current_report_not_located | [Source / lead](http://www.carrollcountyms.org) |
| 09 | Chickasaw | current_report_not_located | [Source / lead](http://www.chickasawcoms.com/) |
| 10 | Choctaw | official_contact_only | [Source / lead](https://www.sos.ms.gov/MSCountyElectionInfo/) |
| 11 | Claiborne | current_report_not_located | [Source / lead](https://www.ccmsgov.us/election-commission/) |
| 12 | Clarke | current_report_not_located | [Source / lead](https://www.clarkecountyms.gov/election-commission/) |
| 13 | Clay | website_placeholder | [Source / lead](https://www.claycountyms.com/election-results/) |
| 14 | Coahoma | current_report_not_located | [Source / lead](https://coahomacounty.net/circuit-clerk/) |
| 15 | Copiah | current_report_not_located | [Source / lead](https://www.copiahcountyms.gov/elected-officials/election-commission/) |
| 16 | Covington | current_report_not_located | [Source / lead](https://www.covingtoncountyms.gov/government/circuit-clerk) |
| 17 | DeSoto | verified_current_source | [Source / lead](https://www.desotocountyms.gov/AlertCenter.aspx?AID=Unoffical-Election-Results-for-November--360) |
| 18 | Forrest | current_report_not_located | [Source / lead](https://forrestcountyms.us/) |
| 19 | Franklin | current_report_not_located | [Source / lead](https://franklincoms.weebly.com/county-government.html) |
| 20 | George | current_report_not_located | [Source / lead](https://www.georgecountyms.com/circuit_clerk.html) |
| 21 | Greene | current_report_not_located | [Source / lead](https://www.greenecountyms.gov/about-5-1) |
| 22 | Grenada | official_contact_only | [Source / lead](https://www.sos.ms.gov/MSCountyElectionInfo/) |
| 23 | Hancock | verified_current_source | [Source / lead](https://www.hancockcounty.ms.gov/DocumentCenter/Index/164) |
| 24 | Harrison | current_report_not_located | [Source / lead](https://harrisoncountyms.gov/government/election_commission/index.php) |
| 25 | Hinds | verified_current_source | [Source / lead](https://www.co.hinds.ms.us/pgs/results/ElectionNightResults.asp) |
| 26 | Holmes | current_report_not_located | [Source / lead](https://holmescountyms.org/election-commission) |
| 27 | Humphreys | current_report_not_located | [Source / lead](https://www.humphreyscountyms.com/supervisors/index.php) |
| 28 | Issaquena | official_contact_only | [Source / lead](https://www.sos.ms.gov/MSCountyElectionInfo/) |
| 29 | Itawamba | official_contact_only | [Source / lead](https://www.sos.ms.gov/MSCountyElectionInfo/) |
| 30 | Jackson | older_results_only | [Source / lead](https://co.jackson.ms.us/Archive.aspx?AMID=42) |
| 31 | Jasper | current_report_not_located | [Source / lead](https://co.jasper.ms.us/circuit-clerk/) |
| 32 | Jefferson | current_report_not_located | [Source / lead](https://www.jeffersoncountyms.com/circuit-clerk/) |
| 33 | Jefferson Davis | current_report_not_located | [Source / lead](http://www.jeffersondaviscountyms.com/) |
| 34 | Jones | current_report_not_located | [Source / lead](https://jonescounty.com/) |
| 35 | Kemper | current_report_not_located | [Source / lead](https://kempercountyms.gov/circuit-clerk/) |
| 36 | Lafayette | verified_current_source | [Official report](https://lafayettems.com/wp-content/uploads/2026/11/2026-GENERAL-ELECTION-UNOFFICIAL-RESULTS-AFFIDAVITS-NOT-COUNTED.pdf) |
| 37 | Lamar | verified_current_source | [Official report](https://lamarcountyms.gov/ms/wp-content/uploads/2026/11/Election_Summary_Results3.pdf) |
| 38 | Lauderdale | verified_current_source | [Source / lead](https://www.lauderdalecounty.org/tate-wins-circuit-clerk-other-races-too-close-to-call/) |
| 39 | Lawrence | current_report_not_located | [Source / lead](https://lawrencecountyms.com/board-of-superviors/) |
| 40 | Leake | older_results_only | [Source / lead](https://leakecountyms.org/election-results/) |
| 41 | Lee | current_report_not_located | [Source / lead](http://leecoms.com/) |
| 42 | Leflore | current_report_not_located | [Source / lead](https://www.leflorecounty.ms/elected-offices/election-commission) |
| 43 | Lincoln | current_report_not_located | [Source / lead](https://www.golincolnms.com/circuit-clerk) |
| 44 | Lowndes | current_report_not_located | [Source / lead](https://www.lowndescountyms.com/m/directory/department?did=18) |
| 45 | Madison | verified_current_source | [Source / lead](https://www.madison-co.com/) |
| 46 | Marion | current_report_not_located | [Source / lead](https://www.marioncountyms.com/election-commission) |
| 47 | Marshall | website_placeholder | [Source / lead](https://www.marshall-county.com/) |
| 48 | Monroe | verified_current_source | [Source / lead](https://www.monroems.com/circuitclerk/results.php) |
| 49 | Montgomery | current_report_not_located | [Source / lead](http://www.montgomerycountyms.com/) |
| 50 | Neshoba | verified_current_source | [Source / lead](https://neshobacounty.net/) |
| 51 | Newton | current_report_not_located | [Source / lead](https://www.newtoncountyms.net/election-commission/) |
| 52 | Noxubee | current_report_not_located | [Source / lead](https://noxubeealliance.com/quality-of-life-noxubee-mississipp/noxubee-county-government/) |
| 53 | Oktibbeha | current_report_not_located | [Source / lead](http://www.oktibbehacountyms.org/) |
| 54 | Panola | current_report_not_located | [Source / lead](http://panolams.com/) |
| 55 | Pearl River | application_needs_investigation | [Source / lead](https://www.pearlrivercounty.net/AlertCenter.aspx?AID=Live-election-results-will-be-available--27) |
| 56 | Perry | current_report_not_located | [Source / lead](https://members.theadp.com/list/member/perry-county-board-of-supervisors-new-augusta-744) |
| 57 | Pike | verified_current_source | [Source / lead](https://www.pikecountyelectionresults.org/2026_general.php) |
| 58 | Pontotoc | current_report_not_located | [Source / lead](http://pontotoccoms.com/) |
| 59 | Prentiss | current_report_not_located | [Source / lead](https://www.prentisscounty.org/circuit-clerk) |
| 60 | Quitman | current_report_not_located | [Source / lead](https://quitmancountyms.org/government/election-comission/) |
| 61 | Rankin | verified_current_source | [Source / lead](https://www.rankincounty.org/topic/index.php?topicid=76&structureid=3) |
| 62 | Scott | current_report_not_located | [Source / lead](https://scottcountyms.gov/government/circuit-clerk/) |
| 63 | Sharkey | current_report_not_located | [Source / lead](https://www.sharkeycounty.net/election-commission/) |
| 64 | Simpson | current_report_not_located | [Source / lead](https://simpsoncountyms.com/how-do-i/elections/) |
| 65 | Smith | current_report_not_located | [Source / lead](https://www.smithcountyms.gov/election-commission/) |
| 66 | Stone | current_report_not_located | [Source / lead](https://www.stonecountyms.gov/election-commission/) |
| 67 | Sunflower | verified_current_source | [Official report](https://www.sunflowercircuitclerk.com/hart/ALL_PRECINCTS.html) |
| 68 | Tallahatchie | official_contact_only | [Source / lead](https://www.sos.ms.gov/MSCountyElectionInfo/) |
| 69 | Tate | current_report_not_located | [Source / lead](https://tatecountyms.gov/circuit-clerk/) |
| 70 | Tippah | fetch_unavailable | [Source / lead](https://www.co.tippah.ms.us/election-results/) |
| 71 | Tishomingo | current_report_not_located | [Source / lead](https://www.co.tishomingo.ms.us/election-commission/) |
| 72 | Tunica | current_report_not_located | [Source / lead](https://tunicacountyms.com/) |
| 73 | Union | current_report_not_located | [Source / lead](http://www.unioncoms.com/) |
| 74 | Walthall | current_report_not_located | [Source / lead](https://www.co.walthall.ms.us/) |
| 75 | Warren | verified_current_source | [Source / lead](https://warrencountyms.gov/documents/) |
| 76 | Washington | current_report_not_located | [Source / lead](https://www.washingtoncounty.ms/circuit-clerk) |
| 77 | Wayne | current_report_not_located | [Source / lead](https://www.waynecounty.ms/election-commission/) |
| 78 | Webster | current_report_not_located | [Source / lead](https://www.webstercountyms.org/node/35) |
| 79 | Wilkinson | current_report_not_located | [Source / lead](https://www.wilkinson.co.ms.gov/voter-registrationelections) |
| 80 | Winston | current_report_not_located | [Source / lead](http://www.winstonms.com/) |
| 81 | Yalobusha | current_report_not_located | [Source / lead](http://www.yalobushaonline.org/) |
| 82 | Yazoo | current_report_not_located | [Source / lead](https://www.yazoocounty.net/circuit-clerk/) |

## Authorities

- https://www.sos.ms.gov/MSCountyElectionInfo/
- https://www.mssupervisors.org/mississippi-counties
- https://www.sos.ms.gov/news/official-statement-election-update

## Later additions

Lafayette, Lamar and Sunflower were added to the configured reports. See [collection and intake](mississippi-collection.md) for exact source limitations and the reviewed-file process for the remaining 69 counties. Historical probe outcomes above retain their original timestamps; absence of a report in that pass does not prove that none exists. The machine-readable `intake` field is the current implementation status for each county.
