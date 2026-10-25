# Development and releasing

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate      # any Python 3.12+ venv
pip install -e ".[dev,snmp]"                            # or: make install
pip install git+https://github.com/turnup-dev/simhall   # optional: the integration tests need it
make test         # pytest -q: unit tests always; integration tests run when simhall is importable
make lint         # ruff check + ruff format --check
```

Library use: `turnup_check.engine.check(intent, collector, creds, timeout=3.0)` runs the same two-pass
check the CLI runs and returns `(Observations, DiffResult)`; `turnup_check.intent.load_intent`,
`turnup_check.collectors.HttpCollector`, `turnup_check.diff.diff` and `turnup_check.report.build_report` are
the pieces.

Tests use canned vendor payloads under `tests/fixtures/` (regenerate with `python tests/fixtures/make_fixtures.py`)
and a fake collector built from an intent, so the diff engine is tested for every finding class and every fix
sentence without network; the telemetry tests post to a local stub that enforces the counter's rules.
`tests/integration/` runs against `simhall` and skips cleanly when it is not installed: the golden, chaos and
scale tiers start `simhall serve` as a subprocess on a free `127.0.0.1` port and run the CLI against it; the
property tier (hypothesis, random halls times random fault sets, expected multiset in the module docstring)
drives the production collector through simhall's ASGI app in-process.

CI (`.github/workflows/ci.yml`) runs on every push to `main` and every pull request, on Python 3.12 and 3.13:
the `test` job runs `ruff check`, `ruff format --check` and the unit tests (`pytest -m "not integration"`)
and checks that the sdist and wheel build; the `integration` job installs simhall from
`git+https://github.com/turnup-dev/simhall@main` and runs the whole suite, integration tier included.

## Releasing

`.github/workflows/release.yml` publishes to PyPI when a `v*` tag is pushed. It checks that the tag
matches `version` in `pyproject.toml` and `__version__` in `src/turnup_check/__init__.py`, builds the
sdist and wheel with `python -m build`, and uploads them with `pypa/gh-action-pypi-publish` through PyPI
trusted publishing (OIDC: no API token is stored anywhere). A tag for a version that is already on PyPI
(uploaded by hand with twine) publishes nothing and passes. The NetBox plugin under `netbox_plugin/` is a
separate distribution and is not built or published by it.

Before the first tag, the PyPI trusted publisher must be configured: on pypi.org, Account settings,
Publishing ("Add a new pending publisher" while the project does not exist yet; the project's Settings,
Publishing afterwards) with PyPI project name `turnup-check`, owner `turnup-dev`, repository name
`turnup-check`, workflow name `release.yml` and environment name `pypi`. GitHub creates the `pypi`
environment on the first run; add required reviewers to it under Settings, Environments if releases should
wait for a click. Tag only while the repository is public (GitHub Free offers environments on public
repositories only).

```bash
# bump the version in pyproject.toml and src/turnup_check/__init__.py, move the [Unreleased] entries
# of CHANGELOG.md under the new version, commit, then:
git tag v0.1.1 && git push origin v0.1.1
```

## Status and known gaps (v0.1.0)

- The NVUE interface document shape follows the public NVUE REST docs and has not been verified on a real
  switch; the SNMP walker (pysnmp) is exercised only through canned OID tables.
- The NetBox and Nautobot adapters have run against the public demos (NetBox 4.7.3, Nautobot 3.2.7), not
  yet against a production GPU hall. Not exercised there: cables with more than one termination per side
  (only the first is read), and whether Nautobot's `location=` filter pulls in child locations.
- Devices without a name in NetBox are kept as `device-<id>` but lose their interfaces and cables.
- Redfish component matching is by `FirmwareInventory` member name; some BMCs name things differently and
  will show as "unknown" until their names are added.
