# Official feed fixtures

Captured November 3, 2026 using an identifying pipeline User-Agent.
MN and NE vote counts were all zero.
No fixture establishes live-result accuracy. Tests create clearly synthetic positive
counts in memory to exercise reconciliation and publication.

Minnesota files are unmodified semicolon text from
https://electionresults.sos.mn.gov/Select/MediaFiles/Index?ersElectionId=201 .
Nebraska CSV is unmodified from
https://electionresults.nebraska.gov/resultsCSV.aspx?map=CTY&text=All&type=SW .

Tests add the Minnesota governor files and Nebraska House rows synthetically
(`tests/test_full_races.py`), with clearly fictitious candidates.
