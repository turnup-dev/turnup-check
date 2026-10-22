# Nautobot public demo fixtures

Captured from the public Nautobot demo on **2026-10-16**.

- Host: `https://demo.nautobot.com`
- Version: **Nautobot 3.2.7** (`api-version: 3.2`), Django 5.2.18 (from `/api/status/`).
  (The adapter was first written against the Nautobot 2.x docs; this live instance is 3.x, and every
  assumption was re-checked against its live JSON.)
- Data resets nightly (banner: "data in this demo instance is reset nightly").
- Credentials `demo` / `nautobot` are documented on the demo login page. **No login or write was
  performed.** The static API token documented in the pynautobot docs for `demo.nautobot.com`
  (the value `"a" * 40`) works for read with `Authorization: Token …` (HTTP 200), so it was used
  read-only. The token value is not stored in this repo.
- Two locations chosen = the two with the most cables of the 125 locations:
  `FRA01` (78 cables) and `ASH01` (74 cables). `TYO01` also has 74. Totals across the instance:
  1311 devices, 24684 interfaces, 3686 cables.

## Files

First API page of each endpoint, exactly as returned, one set per location
(`?location=<name>&limit=1000`, at the depth the adapter requested on 2026-10-16; the current adapter
reads devices at `depth=2` with `include=config_context` and cables at `depth=1`, CHANGELOG.md D1, D2, D10):

- `<loc>_devices.json`         — `GET /api/dcim/devices/?location=<loc>&limit=1000&depth=1` (adapter's query)
- `<loc>_devices_depth2.json`  — same at `depth=2` (evidence for D1; shows `manufacturer.name`)
- `<loc>_interfaces.json`      — `GET /api/dcim/interfaces/?location=<loc>&limit=1000&depth=1` (FIRST page: 100 of 354; the demo caps page size at 100 and the adapter follows `next`)
- `<loc>_interfaces_all.json`  — all 354 interfaces (every `next` page concatenated); this is what `report-<loc>.json` was built from, so the reports are reproducible from the saved fixtures
- `<loc>_cables.json`          — `GET /api/dcim/cables/?location=<loc>&limit=1000&depth=2` (adapter's query; complete, 74/78)
- `<loc>_locations.json`       — `GET /api/dcim/locations/?name=<loc>`
- `<loc>_racks.json`           — `GET /api/dcim/racks/?location=<loc>&limit=1000`
- `report-<loc>.json`          — turnup-check report (see note below)

## The live CLI run timed out (D10, fixed before 0.1.0)

On 2026-10-16, `turnup-check --site <loc> --intent nautobot --only lldp --timeout 1` **failed** on this
demo with `reading intent failed: nautobot: ReadTimeout` (exit 2). The cables query at
`depth=2&limit=1000` took **80–177 s** server-side (measured: ASH01 ~81 s, FRA01 ~177 s), far beyond the
adapter's hard-coded 10 s per-request default at the time, and `--timeout` was not plumbed into intent
loading. The adapter now reads cables at `depth=1` and allows `max(--timeout, 120)` s per intent request.

`report-<loc>.json` was therefore produced by the same adapter + engine code on these captured
pages (devices + cables as saved; interfaces from `<loc>_interfaces_all.json`), not by a single
live CLI invocation. It is faithful to the live data and reproducible from the fixtures here. All
switches in it are polled as `snmp` because the adapter's `depth=1` device query of the time left
`manufacturer` empty (D1, since fixed).

The JSON pages are used unchanged by `tests/test_intent_demo_fixtures.py`, which serves
`<loc>_interfaces_all.json` in pages of 100, and the `depth=2` cable page reduced to the `depth=1` shape
when the adapter asks for `depth=1`.
