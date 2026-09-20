# Repository Guidelines

## Project Structure & Module Organization

This repository builds and explores a historical NIC Argentina RDList dataset.

- `scripts/update_db.py` downloads, validates, normalizes, and migrates the DuckDB database.
- `scripts/prepare_web_data.py` copies the database as a static asset named with the first 10 characters of its SHA-256 and writes its manifest.
- `tests/test_update_db.py` contains Python tests for parsing, schema migration, and transactional imports.
- `data/` holds the local `rdlist.duckdb` database. Database files are generated and ignored by Git; retain `data/.gitkeep` only.
- `web/` is the Vite + React + TypeScript viewer. Application code is in `web/src/`; production output in `web/dist/` is generated.

## Build, Test, and Development Commands

Install the Python dependencies with `uv venv && uv pip install -r requirements.txt` and JavaScript dependencies with `npm install`.

- `npm test` runs the Python test suite quietly with pytest.
- `uv run --with-requirements requirements.txt python scripts/update_db.py bootstrap` downloads the complete dataset into `data/rdlist.duckdb`.
- `uv run --with-requirements requirements.txt python scripts/update_db.py latest` refreshes the most recent listing pages; use `migrate` for an offline schema upgrade.
- `npm run build` copies the DuckDB as a content-addressed asset, writes `rdlist.json`, type-checks the web app, and produces `web/dist/`.
- `npx serve web/dist` serves the production build for local testing.

## Coding Style & Naming Conventions

Follow the existing conventions: Python uses four-space indentation, type annotations, `snake_case` functions and variables, and concise docstrings. Keep import logic transactional and preserve source SHA-256 validation. TypeScript uses two-space indentation, single quotes, semicolon-free statements, `camelCase` values/functions, and `PascalCase` React components. Keep UI copy in Spanish to match the existing interface.

No formatter or linter is configured. Match adjacent code and run the relevant tests and build before submitting changes.

## Testing Guidelines

Add pytest tests in `tests/test_<feature>.py`, naming cases `test_<behavior>`. Prefer isolated fixtures and temporary databases (`tmp_path`) over the checked-in local dataset. Cover parsing edge cases, hash failures, and database rollback behavior when modifying ingestion or migrations. Run `npm test`; for frontend changes also run `npm run build`.

## Commit & Pull Request Guidelines

Git history is not available in this checkout, so use concise imperative commit subjects, such as `Normalize empty domain zones`. Keep each commit focused. Pull requests should explain the user-visible or data-model impact, list validation commands, link the relevant issue when available, and include screenshots for UI changes. Do not commit `node_modules/`, `web/dist/`, caches, or generated DuckDB files.

## Security & Configuration

Keep TLS verification enabled when updating data. Use `--ca-bundle` for a trusted custom certificate; reserve `--insecure` for temporary local troubleshooting.
