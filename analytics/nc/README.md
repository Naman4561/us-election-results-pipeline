# North Carolina results analysis

Open `index.html` in a browser and select the JSON export from `export.sql`.
This view uses frozen database publications from the November 3, 2026 election.
The file stays in the browser. The page can be opened from disk.

## Export from Lightsail

Copy `export.sql` to your Ubuntu home directory as `export_nc_analysis.sql`.
Run this in the same SSH session where your production Compose helper `dc` is available:

```bash
dc exec -T db sh -c 'psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$HOME/export_nc_analysis.sql" > "$HOME/nc-analysis.json"
```

If the helper is unavailable, use the production Compose configuration directly:

```bash
sudo docker compose --env-file /etc/election-pipeline/production.env -f /opt/election-pipeline/compose.production.yml exec -T db sh -c 'psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$HOME/export_nc_analysis.sql" > "$HOME/nc-analysis.json"
```

Copy `nc-analysis.json` to your computer. The export is a read-only transaction
with statement and lock timeouts. It queries the already migrated `nc` schema.
The exporter can also run against your local Docker Compose PostgreSQL instance.

## Reading the dashboard

- **Snapshot inventory** reports database registrations, loaded releases, publication counts,
  and capture/check timestamps by source. A registered archive is a database record.
  Check the server's `data/raw/NC/2026-11-03/` directory for physical file counts.
- **Results history** uses one validated `unofficial` publication per retained source
  snapshot, selecting the newest transform for that snapshot. Original and expanded NC
  sources stay separate so overlapping releases cannot be added together.
- **Snapshot time** is the first observed capture time where available, with publication
  time as a fallback. Points represent file versions retained by the pipeline. They
  cannot reconstruct intervening updates between checks or reappearances of identical bytes.
- **Net change** compares two adjacent retained snapshots with comparable candidate sets.
  It can include corrections. County rows in a House contest cover the district portion.
- **Ballot demographics** uses the latest validated `unofficial` demographic publication
  per source. Countywide/NC-wide totals cover the selected request type and dimension.
  These are group-level ballot counts and party registration. Candidate votes and demographics
  have separate timestamps and separate grains.

The raw precinct ZIP includes the full official file. The parsed results retain selected
federal races and include the administrative reporting units needed to reconcile totals.
This dashboard starts with the validated county and contest aggregates. Precinct detail
remains in `nc.results` by release. Live reporting-unit IDs can include early voting sites
and other administrative units. A residence-precinct analysis requires the separate
post-election precinct-sorted product.
