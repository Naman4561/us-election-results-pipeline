# State status

Where each state's 2026 general election results stand. Updated by hand; last updated
**November 10, 2026, 03:50 UTC**. [States](states.md) describes each source.

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
| **Not covered** | No official source we can use. |

"Last vote change" is when we first saw a race total change, in UTC.

| State | Status | Last vote change | Notes |
| --- | --- | --- | --- |
| Alaska | Unofficial, between counts | Nov 5 01:45 | Counts scheduled Nov 10, 13 and 18; certification targeted Nov 25. |
| California | Unofficial, counting | Nov 9 22:52 | |
| Colorado | Unofficial, counting | Nov 8 01:17 | Counties count through about Nov 12. |
| Florida | Unofficial, paused | Nov 7 05:21 | Florida's file host serves an expired certificate. Totals match floridaelectionwatch.gov. Resume with `deploy resume --source fl_2026_general`. |
| Georgia | Unofficial, counting | Nov 9 23:06 | |
| Hawaii | Unofficial, pending certification | Nov 6 17:27 | Collected hourly. |
| Idaho | Unofficial, counting | — | Public since Nov 10 03:22. Totals matched Idaho's site at activation. Certification targeted Nov 24. |
| Iowa | Unofficial, pending certification | Nov 6 17:23 | State's last update Nov 4, 12:25 PM CST. Collected hourly. |
| Kansas | Final unofficial | Nov 9 02:43 | Imported by hand. |
| Kentucky | Waiting for certification | — | Live feed retired and closed to automated clients; our only snapshot predates poll close. Import certified results from elect.ky.gov when posted. |
| Maryland | Unofficial, counting | Nov 9 18:00 | |
| Minnesota | Unofficial, counting | Nov 9 20:21 | |
| Mississippi | Unofficial, counting | Nov 9 19:38 | County files; Hancock and Pearl River are reviewed manual snapshots. |
| Nebraska | Unofficial, pending certification | Nov 6 23:02 | All precincts in. Collected every 3 hours, clear of backups. |
| New Mexico | Unofficial, pending certification | Nov 6 17:00 | State's last update Nov 6, 9:55 AM MT. Collected hourly. |
| North Carolina | Unofficial, counting | Nov 9 17:21 | |
| Ohio | Unofficial, counting | Nov 4 18:50 | Imported by hand; the Nov 4 snapshot is the latest, and the monitor reports a newer one as overdue. |
| Oregon | Unofficial, counting | Nov 9 18:47 | |
| South Carolina | Unofficial, pending certification | Nov 9 21:41 | State's last update Nov 6, 6:26 PM EST; the Nov 9 change is the move to its Clarity site. |
| South Dakota | Unofficial, pending certification | Nov 6 20:56 | All precincts in. Collected every 3 hours, clear of backups. |
| Texas | Unofficial, pending certification | Nov 4 22:21 | All 254 counties in; state's last update Nov 4, 22:48. Collected every 3 hours, clear of backups. |
| Virginia | Not public yet | — | Current data passes every check; ready to add. |
| Washington | Unofficial, counting | Nov 9 23:06 | Counts on weekdays. |
| West Virginia | Unofficial, counting | Nov 9 22:46 | |
| Maine | Not covered | — | Municipal reporting with no statewide endpoint. |
| Montana | Not covered | — | Results pages marked "TEST"; export fails on about half of requests. |
