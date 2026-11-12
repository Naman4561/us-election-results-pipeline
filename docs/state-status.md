# State status

Where each state's 2026 general election results stand. Updated by hand; last updated
**November 12, 2026, 16:40 UTC**. [States](states.md) describes each source.

| Status | Meaning |
| --- | --- |
| **Official** | The state certified its results; we publish them and no longer collect. |
| **Unofficial, counting** | The state is still posting new counts; we collect them as they appear. |
| **Unofficial, between counts** | The state counts on a published schedule; the next count is dated. |
| **Unofficial, pending certification** | Was live, but the state's count has stopped. Collected less often until the state certifies or posts more. |
| **Unofficial, paused** | Collection is stopped by an operator (`deploy pause`); the last publication stays public. |
| **Final unofficial** | The count is complete and imported by hand; nothing more is expected before certification. |
| **Waiting for certification** | No usable results yet; the next step is the state's certified results. |
| **Not public yet** | Configured, but not on the public dashboard. |
| **Awaiting certified results** | No election-night source we collect; added from the state's certified canvass. |
| **Not covered** | No official source we can use. |

"Last vote change" is when we first saw a race total change, in UTC.

| State | Status | Last vote change | Notes |
| --- | --- | --- | --- |
| Alaska | Unofficial, between counts | Nov 12 03:46 | Counts scheduled Nov 13 and 18; certification targeted Nov 25. |
| Arkansas | Unofficial, counting | Nov 12 14:31 | All 2,768 precincts in. |
| California | Unofficial, counting | Nov 12 05:02 | |
| Colorado | Unofficial, counting | Nov 11 00:56 | Counties count through about Nov 12. |
| Delaware | Official | Nov 10 19:30 | Certified; collection paused. |
| Florida | Unofficial, paused | Nov 7 05:21 | Florida's file host serves an expired certificate. Totals match floridaelectionwatch.gov. Resume with `deploy resume --source fl_2026_general` once it is renewed. |
| Georgia | Unofficial, counting | Nov 10 21:00 | |
| Hawaii | Unofficial, pending certification | Nov 11 06:38 | Collected hourly. |
| Idaho | Unofficial, counting | Nov 11 05:20 | Certification targeted Nov 24. |
| Indiana | Unofficial, counting | Nov 10 20:11 | Its settings carry a certified flag. |
| Iowa | Unofficial, pending certification | Nov 6 17:23 | State's last update Nov 4, 12:25 PM CST. Collected hourly. |
| Kansas | Final unofficial | Nov 9 02:31 | Imported by hand. |
| Kentucky | Waiting for certification | — | Live feed retired and closed to automated clients; our only snapshot predates poll close. Import certified results from elect.ky.gov when posted. |
| Louisiana | Unofficial, counting | Nov 10 11:11 | House districts 5 and 6 go to a December runoff. |
| Maryland | Unofficial, counting | Nov 12 12:46 | |
| Minnesota | Unofficial, counting | Nov 10 21:21 | |
| Mississippi | Unofficial, counting | Nov 12 04:02 | County files. Madison now posts its official text report, read automatically. Pearl River (official Nov 10 report) and Hancock (Nov 3 report, re-checked Nov 12) are reviewed manual snapshots. The state certifies Nov 13: import its recap ([Mississippi](mississippi-collection.md#certification)). |
| Nebraska | Unofficial, pending certification | Nov 6 23:02 | All precincts in. Collected every 3 hours, clear of backups. |
| New Mexico | Unofficial, pending certification | Nov 6 17:00 | State's last update Nov 6, 9:55 AM MT. Collected hourly. |
| North Carolina | Unofficial, counting | Nov 11 17:01 | |
| North Dakota | Unofficial, counting | Nov 10 05:18 | All 358 precincts in. |
| Ohio | Final unofficial | Nov 12 03:26 | Imported by hand; the Nov 11, 10:24 PM EST report is the last before certification (re-checked Nov 12, 10:16 AM EST); marked final with `manual final` from the next release. |
| Oklahoma | Unofficial, counting | Nov 11 01:58 | County results export imported by hand. |
| Oregon | Unofficial, counting | Nov 11 03:41 | |
| Rhode Island | Unofficial, counting | Nov 10 21:35 | Statewide and district totals only (reported by town). |
| South Carolina | Unofficial, pending certification | Nov 10 22:26 | |
| South Dakota | Unofficial, pending certification | Nov 6 20:56 | All precincts in. Collected every 3 hours, clear of backups. |
| Texas | Unofficial, pending certification | Nov 4 22:21 | All 254 counties in; state's last update Nov 4, 22:48. Collected every 3 hours, clear of backups. |
| Utah | Unofficial, counting | Nov 12 02:32 | |
| Vermont | Official | Nov 10 17:54 | Certified Nov 12 16:35 from its feed's official marker, checked against the state's results; collection paused. Map and results by town. |
| Virginia | Not public yet | — | Current data passes every check; ready to add. |
| Washington | Unofficial, counting | Nov 11 23:47 | Counts on weekdays. |
| West Virginia | Unofficial, counting | Nov 10 16:01 | |
| Wyoming | Official | Nov 10 21:37 | Added from its certified results (certified Nov 10). |
| Maine | Awaiting certified results | — | Municipal reporting with no statewide endpoint. |
| Montana | Awaiting certified results | — | Results pages marked "TEST"; export fails on about half of requests. |
| Other states holding elections | Awaiting certified results | — | No election-night source collected; see [states](states.md#awaiting-certified-results). |
