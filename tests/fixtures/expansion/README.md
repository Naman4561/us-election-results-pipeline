# Official feed fixtures

Captured November 3, 2026 using an identifying pipeline User-Agent.
MN and NE vote counts were all zero. TX vote cells were blank, not zero.
No fixture establishes live-result accuracy. Tests create clearly synthetic positive
counts in memory to exercise reconciliation and publication.

Minnesota files are unmodified semicolon text from
https://electionresults.sos.mn.gov/Select/MediaFiles/Index?ersElectionId=201 .
Nebraska CSV is unmodified from
https://electionresults.nebraska.gov/resultsCSV.aspx?map=CTY&text=All&type=SW .
Texas fixture retains literal selected table markup from
https://electionresults.sos.state.tx.us/results.html . Other contests, layout and
scripts were omitted; the election heading is retained as text.
The original whole-page SHA-256 was
`136d28f6a36c76f7d27b1f25bd5c8a5f14a2c4f2756ac2f8cd54c4f7ab79b73c`.
