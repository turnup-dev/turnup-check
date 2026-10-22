# NetBox public demo fixtures

Captured from the public NetBox demo on **2026-10-16**.

- Host: `https://demo.netbox.dev`
- Version: **NetBox 4.7.3** (`API-Version: 4.7`), Python 3.12.3 (from `/api/status/`)
- Data resets nightly at **04:00 UTC** (so these pages are only reproducible until the next reset).
- Two sites chosen = the two with the most cables of the 31 sites:
  `ncsu-065` (15 cables) and `dm-buffalo` (7 cables).

## How the token was obtained (the only write made)

`admin/admin` is accepted by this demo (the demo's own login page advertises a personal-account
flow at `/plugins/demo/login/`, but `admin/admin` also works). One API token was provisioned with
those credentials through the REST API's token-provision endpoint (`tokens/provision/` in the `users`
app), which answered 201, and was then used for reads only.

That returned a **v2** token (id 15, user `admin`, `write_enabled`, no expiry). The token value is
**not** stored in this repo. No other object was created, modified or deleted.

Note on the v2 token shape: the provision response returns the secret split as `key` (12 chars) +
`token` (40 chars); the usable plaintext is `nbt_<key>.<token>` (57 chars). **On NetBox 4.7.3 that
plaintext authenticates under BOTH `Authorization: Token …` and `Authorization: Bearer …` (both HTTP
200)**, so turnup-check keeps sending `Authorization: Token` (CHANGELOG.md, D3).

## Files

First API page of each endpoint, exactly as returned (`?site=<slug>&limit=1000`), one set per site:

- `<site>_devices.json`      — `GET /api/dcim/devices/?site=<slug>&limit=1000`
- `<site>_interfaces.json`   — `GET /api/dcim/interfaces/?site=<slug>&limit=1000`
- `<site>_cables.json`       — `GET /api/dcim/cables/?site=<slug>&limit=1000`
- `<site>_racks.json`        — `GET /api/dcim/racks/?site=<slug>&limit=1000`
- `<site>_sites.json`        — `GET /api/dcim/sites/?slug=<slug>`
- `report-<site>.json`       — `turnup-check --site <slug> --intent netbox --only lldp --timeout 1`

For NetBox these pages are complete (`count` == `len(results)`; `next` is null). NetBox returns
`device_type.manufacturer.name` and nested cable terminations by default, so the adapter needs no
`depth` parameter here.

The JSON pages are used unchanged by `tests/test_intent_demo_fixtures.py`.
