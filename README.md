# us-election-results-pipeline

Collection, validation, publication and a public dashboard of official 2026 U.S.
general-election results: every Senate, Governor and House race of 24 states, taken
only from each state's own election office.

| Read | For |
| --- | --- |
| [docs/architecture/](docs/architecture/README.md) | How data moves from official files to the dashboard |
| [docs/states.md](docs/states.md) | Each state's source, races, county detail, vote methods and known limits |
| [docs/adding-a-state.md](docs/adding-a-state.md) | Adding a state: registry entry, parser, config, roster, map |
| [docs/deployment.md](docs/deployment.md) | Staging and switching releases, adding a state in production, rollback |
| [docs/manual-results.md](docs/manual-results.md) | Importing an official file saved by hand (Ohio, Kansas) |
| [docs/mississippi-collection.md](docs/mississippi-collection.md) | Mississippi's county-by-county collection and reviewed imports |
| [docs/backups.md](docs/backups.md) | Backups to S3, retention and restore checks |

## Local checks

```powershell
.\.venv\Scripts\python -m pip install -c constraints.txt -e '.[postgres,transform,dashboard,deployment]' -r requirements-dev.txt
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m unittest discover -s tests
# With a PostgreSQL 17 server in .env: the database, dbt and publication tests too.
.\.venv\Scripts\python scripts/check_pipeline.py
```
