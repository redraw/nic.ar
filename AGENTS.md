# Repository Guidelines

## Project Structure & Module Organization

This repository maintains the historical NIC Argentina RDList dataset as a normalized DuckDB database.

- `scripts/update_db.py` is the application entry point. It discovers RDList listings, downloads CSVs, verifies their published SHA-256 hashes, and imports them transactionally.
- `data/` is the local working location for `rdlist.duckdb`. Database files are generated and ignored; retain `data/.gitkeep` only.
- `.github/workflows/update-db.yml` runs the scheduled/manual refresh. It restores the canonical database from Cloudflare R2, updates it, and uploads it only when the dataset changed.
- `requirements.txt` declares the runtime dependencies: DuckDB and Requests.

The database contains `sources` (one validated NIC CSV per source date), `domain_records` (the normalized rows), and the `dataset_status` view. Legacy `records` data is migrated into `domain_records` and then removed.

## Build, Test, and Development Commands

Create an isolated Python environment with `uv venv && uv pip install -r requirements.txt`.

- `uv run --with-requirements requirements.txt python scripts/update_db.py bootstrap` downloads and imports the full published history into `data/rdlist.duckdb`.
- `uv run --with-requirements requirements.txt python scripts/update_db.py latest` refreshes the three most recent listing pages; pass `--recent-pages N` to change that window.
- `uv run --with-requirements requirements.txt python scripts/update_db.py migrate` performs the offline schema migration only.
- Add `--database PATH` to operate on a temporary or alternate database, and `--log-level DEBUG` for diagnostic logging.

The updater finishes by printing `changed=true` or `changed=false`; the GitHub Actions workflow relies on that exact output. There is currently no committed test suite, JavaScript application, or frontend build command.

## Coding Style & Naming Conventions

Use Python 3.12-compatible code, four-space indentation, type annotations, `snake_case` names, and concise docstrings. Match the compact style already used in `scripts/update_db.py`.

Preserve the ingestion guarantees: keep TLS verification enabled by default, verify every downloaded CSV against NIC's published SHA-256 before import, and keep source replacement atomic. Treat `--insecure` as temporary local troubleshooting only; use `--ca-bundle` for a trusted custom CA.

Keep `sources` and `domain_records` consistent for each `source_date`. Schema changes belong in `ensure_schema`/`_ensure_schema` and must remain safe for existing databases. Normalize domain and zone values consistently (trim, remove outer dots, lowercase) and avoid turning missing values into misleading empty strings.

## Testing Guidelines

For changes to parsing, normalization, migration, or import logic, exercise the smallest appropriate mode against a temporary database using `--database`; do not overwrite `data/rdlist.duckdb` merely to validate a change. Add focused pytest coverage under `tests/` when introducing or changing behavior, preferably using `tmp_path` databases and mocked HTTP responses.

## Commit & Pull Request Guidelines

Use concise imperative commit subjects. Keep commits focused and describe data-model, ingestion, or automation impact in pull requests, including the commands run. Do not commit generated DuckDB files, credentials, caches, or R2 configuration.

## Security & Configuration

The scheduled workflow runs daily at 08:00 UTC and can be dispatched with `latest` or `bootstrap`. It requires these GitHub configuration values for R2:

- Variables: `R2_BUCKET`, `R2_ACCOUNT_ID`
- Secrets: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`

Keep the workflow's restore-update-upload flow intact so routine updates are incremental and a missing canonical object bootstraps correctly.
