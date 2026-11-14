"""A local workspace for analysing the pipeline's results with outside data.

It never connects to production for queries: ``restore`` copies the newest verified
backup's database into a local PostgreSQL (in Docker), and outside data (FEC, Census,
past results) is downloaded with its provenance into schemas of that same database.
"""
