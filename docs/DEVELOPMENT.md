# Developer notes

This page covers the small amount of engineering hygiene wired into the
project: a linter and a CI workflow. The tool itself is described in the
PRD, Test Plan, and Rollout Plan in this directory.

## Linting (Ruff)

Ruff is a fast Python linter that catches real bugs (undefined names,
unused imports, common Python pitfalls) without enforcing every
stylistic preference.

### Run locally

From the project root:

```bash
# Check for any issues. Exits with code 0 if clean.
python -m ruff check app tests

# Auto-fix the issues that are safe to auto-fix.
python -m ruff check --fix app tests

# Format check (not enforced; opt-in).
python -m ruff format --check app tests
```

### Configuration

Config lives in [`pyproject.toml`](./pyproject.toml) under
`[tool.ruff.lint]`. We enable a conservative set of rules:

- `E9` – syntax errors
- `F` – Pyflakes (undefined names, unused imports, etc.)
- `B` – Bugbear (mutable default args, missing zip strict, etc.)

A small set of stylistic rules are explicitly ignored so the existing
codebase passes without modification. See the comments in
`pyproject.toml` for what's ignored and why. Adding stricter rules
later is fine — just expect to clean up the warnings they surface.

## Tests

The regression suite covers every bug we've fixed. Each new bug should
get a test before its fix is merged.

From the project root:

```bash
python -m unittest discover -s tests -p "test_regression_fixes.py" -v
```

Smoke test on every historical article fixture lives under
`dist/output/runs/` and `Beta test/`. See [`MAINTENANCE.md`](
../MAINTENANCE.md) for the verification scripts.

## CI (GitHub Actions)

The workflow at [`.github/workflows/tests.yml`](
./.github/workflows/tests.yml) runs on every push and pull request:

1. Set up Python 3.11
2. Install runtime + dev dependencies
3. Run `ruff check`
4. Run the regression test suite

If either step fails, the PR is blocked from merging (when branch
protection is on). The workflow is dormant until this repository is
pushed to GitHub — it's safe to commit while the repo lives only
locally.

To run it manually from the GitHub UI: **Actions → Tests → Run workflow**.

## Adding a new dependency

If you `pip install` something new for the tool itself, add it to the
`pip install` line in `.github/workflows/tests.yml` so CI installs it
too.

## When the tool retires

Per the PRD, this is a one-time migration helper. Once the parity
backlog is processed:

- Archive the codebase to a read-only location.
- Disable the CI workflow (delete or rename `tests.yml`).
- The PRD, Test Plan, and Rollout Plan should travel with the
  codebase as a record of why it existed.
