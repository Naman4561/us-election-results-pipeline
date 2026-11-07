# us-election-results-pipeline
Collection, validation, transformation, visualization, and analysis of official U.S. election results.

Nov 2

Backups (every six hours to S3, three days and the newest five retained): [docs/backups.md](docs/backups.md).

Adding a state (registry entry, parser, then generated dbt models and migration): [docs/adding-a-state.md](docs/adding-a-state.md).

Multi-state architecture, Kentucky source contract, validation and rollout: [docs/multistate-expansion.md](docs/multistate-expansion.md).

Adding states without restarting the accepted collectors: [docs/isolated-deployments.md](docs/isolated-deployments.md).

South Dakota, New Mexico and Florida (Senate, governor and House, with county results): [docs/sd-nm-fl-expansion.md](docs/sd-nm-fl-expansion.md).

Virginia, Idaho, Washington, Colorado, Iowa, Oregon, California, Maryland and Hawaii (with vote methods where published; Montana deferred): [docs/nine-state-expansion.md](docs/nine-state-expansion.md).

Operational hardening from the November 2026 review (backoff, retention, dashboard load, builds): [docs/hardening-2026-11.md](docs/hardening-2026-11.md).

Georgia, South Carolina, Kansas and Ohio adapters, validation and isolated rollout;
Maine official-source work remains pending: [docs/five-state-expansion.md](docs/five-state-expansion.md).
