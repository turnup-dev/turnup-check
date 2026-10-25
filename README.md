# turnup-check

```
$ turnup-check --site hall-b --intent netbox
reading intent      288 links · 18 nodes · 2 leaves
polling             lldp · redfish · dcgm
✓ 287 / 288 links match intent
✓ 18 / 18 nodes reachable, firmware baseline ok
✗ mismatch   node7 mlx5_3 → leaf4/22
             expected leaf4/21
fix          move cable one port up
elapsed      4.1 s

report       hall-b-<YYYY-MM-DD>.json
share stats  --telemetry opt-in (racks, links)
```

`turnup-check` reads the intended build of a GPU hall from NetBox or Nautobot (racks, devices, interfaces,
cables), polls what the hall actually looks like (LLDP and port state from the switches, Redfish from the
BMCs, DCGM from the GPUs) and prints the diff: wrong port, missing link, unreachable node, firmware off
baseline, GPU count and health. It writes a JSON report. Nothing else. Any vendor's switches and servers;
Apache-2.0.

Exit code `0` means the hall matches intent, `1` means findings, `2` means the intent could not be read or
no telemetry source answered. The whole run is one process, concurrent, and finishes in seconds on a rack.

## Install

```bash
pip install turnup-check            # Python 3.12+
pip install "turnup-check[snmp]"    # adds pysnmp for switches without eAPI/NVUE
```

Or from a checkout: `pip install -e .`.

## Five minutes on a NetBox site

```bash
pip install turnup-check
export NETBOX_URL=https://netbox.example NETBOX_TOKEN=...
turnup-check --site <slug> --intent netbox
```

Device credentials come from `TURNUP_BMC_USER` / `TURNUP_BMC_PASS` (Redfish on every GPU node) and
`TURNUP_SWITCH_USER` / `TURNUP_SWITCH_PASS` (NVUE or eAPI; they fall back to the BMC pair), or from
`--creds creds.yaml` (format below). A device that refuses or does not answer is reported as unreachable,
never fatal; `--only lldp` is the fastest first run (cabling only, switch credentials only).

The two summary lines always carry the check mark and each finding block the cross, as in the output at the
top of this page: `✓ 287 / 288 links match intent` above `✗ mismatch`.

What it needs in NetBox (3.x or 4.x):

| Object | Fields read |
|---|---|
| `dcim.device` in the site | `name`, `role.slug` (`device_role.slug` on NetBox 3), `device_type.model`, `device_type.manufacturer.name`, `rack.name`, `position`, `serial`, `primary_ip4`, `custom_fields`, `config_context` |
| `dcim.interface` in the site | `device.name`, `name`, `type`, `mac_address` (or `primary_mac_address` on NetBox 4.2+); `virtual`, `lag` and `bridge` interfaces are left out |
| `dcim.cable` in the site | `label`, `a_terminations` / `b_terminations` (or legacy `termination_a` / `termination_b`); only interface-to-interface cables are links |

Roles: `gpu-node` (also accepted: `gpu-server`, `compute-node`), `leaf-switch` (`leaf`, `tor`, `access-switch`),
`spine-switch` (`spine`), `nvlink-switch`, `pdu`, `cdu`. Everything else becomes `other` and is ignored for
polling; each unrecognised role is listed once in the report `notes` with its device count, e.g.
`role 'core-switch' not recognised: 2 device(s) treated as other`.

Notes (in the report's `notes`, and on stderr with `-v`) also explain what was read but left out of intent:

- `cable <label> skipped: not interface-to-interface (a=dcim.powerport, b=dcim.poweroutlet)`: both object
  types are named (front/rear ports, circuit terminations, power ports; `none` when a side has no termination).
- `18 logical interface(s) not in intent (virtual 18)`: virtual, LAG and bridge interfaces never hold a cable.

Requests and limits: every list is read with `limit=1000` and the `next` links are followed (servers that cap
the page size, like the Nautobot demo at 100, are paged through). Intent reads use a per-request read timeout of
`max(--timeout, 120)` seconds and a 10 s connect timeout; `--timeout` (default 3 s) is otherwise the per-poll
deadline for switches, BMCs and DCGM. In Python, `NetBoxIntentSource(..., timeout=, page_limit=)` and
`load_intent(..., timeout=)` take the same values.

Management endpoints, in order of preference: device custom fields `turnup_bmc_url`, `turnup_dcgm_url`,
`turnup_switch_url` (and optional `turnup_protocol` = `nvue` | `eapi` | `snmp`, `turnup_gpu_count`); else
`primary_ip4` as `https://<ip>` for BMCs and switches and `http://<ip>:9400` for the DCGM exporter.

Firmware baseline: `config_context.turnup.firmware_baseline` on the device, a dict of component to version
(`bmc`, `bios`, `nic`, `cx_firmware`, `gpu_vbios`), keyed per device model in the report; or a file:

```bash
turnup-check --site hall-b --intent netbox --baseline baseline.yaml
# baseline.yaml
# "GB300 NVL72 compute tray": {bmc: "1.12.0", bios: "2.3.1", nic: "28.43.1014"}
```

Versions are read as written: an unquoted `bmc: 1.10` stays `1.10` (YAML would make it the number 1.1).

Nautobot (2.x and 3.x): `--intent nautobot` with `NAUTOBOT_URL` / `NAUTOBOT_TOKEN`; the site is a
`location` (matched by name), roles come from `role.name`. Nautobot returns nested objects as bare references
unless asked for `depth`, so the adapter requests:

| Endpoint | Parameters | Why |
|---|---|---|
| `dcim/devices` | `depth=2&include=config_context` | `device_type.manufacturer.name` (picks eAPI / NVUE / SNMP) and `primary_ip4.address`; list views omit `config_context` (the firmware baseline) unless included |
| `dcim/interfaces` | `depth=1` | `device.name` |
| `dcim/cables` | `depth=1` | each end's interface name and device reference, resolved against the device list; `depth=2` took 80-177 s for 74-78 cables on the public demo and timed out the load |
| `dcim/racks`, `dcim/locations` | `depth=1` | names |

Verified against the public NetBox 4.7.3 and Nautobot 3.2.7 demos; the captured pages are in
`tests/fixtures/netbox_demo/` and `tests/fixtures/nautobot_demo/`.

A URL works too: `--intent http://127.0.0.1:8900/netbox` treats it as a NetBox base URL (this is how the
virtual hall below is checked).

A file works as well: `--intent hall-b.yaml` (or `.json`), a `turnup.intent/v1` document. Its schema is the
`Intent` model in `src/turnup_check/models.py` and `tests/fixtures/intent/hall-mini.yaml` is a small example;
`simhall intent --golden hall-b -o hall-b.yaml` (or `hall.write_intent(path)` in Python) writes a full one.

## What is polled

| Source | Protocol | What is read |
|---|---|---|
| Switches | Arista eAPI (`show lldp neighbors detail`, `show interfaces status`), NVIDIA NVUE REST (`/nvue_v1/interface?rev=applied`), or SNMP LLDP-MIB + IF-MIB | per port: link state, speed, LLDP neighbor system and port id (name or MAC) |
| Nodes | DMTF Redfish (`/redfish/v1`): Systems, Managers, UpdateService/FirmwareInventory, Chassis/NetworkAdapters/Ports | health, power state, serial, model, firmware versions by component, NIC port link state and MAC, LLDP as the NIC sees it |
| Nodes | DCGM-exporter `/metrics` (Prometheus text) | GPU count, XID / ECC / row-remap health, NVLink CRC errors and bandwidth |

The transport is chosen by `mgmt.protocol`, else by manufacturer (`Arista` → eAPI; `NVIDIA`, `Cumulus`,
`Mellanox` → NVUE; anything else → SNMP). All devices are polled concurrently: at most 64 requests in flight
overall and 4 per device (`TURNUP_PER_HOST` overrides; BMCs cope badly with more). Devices behind one
`host:port` (a Redfish aggregator, the virtual hall) share 4 live slots, and a request that has not answered
gives its slot back after 0.1 s, so dead devices never queue the live ones. `--timeout` (default 3 s; a
positive number, `nan`/`0`/negatives exit 2) is a deadline per request, body included, so a device that
trickles its answer is cut off too; an unreachable device never stalls the run. The Redfish walk reads Systems,
Managers and FirmwareInventory for every node; the NIC port branch (Chassis/NetworkAdapters/Ports, the most
requests) is fetched in a second pass only for nodes that have a link finding, so a clean hall costs about
ten Redfish requests per node. BMC TLS certificates are not verified unless `TURNUP_TLS_VERIFY=1` (BMCs ship
self-signed certificates).

Credentials: `TURNUP_BMC_USER` / `TURNUP_BMC_PASS`, `TURNUP_SWITCH_USER` / `TURNUP_SWITCH_PASS` (fall back to
the BMC pair), `TURNUP_SNMP_COMMUNITY` (default `public`), or `--creds creds.yaml`:

```yaml
bmc:    {user: admin, password: ...}
switch: {user: admin, password: ...}
snmp:   {community: public, version: 2c}
devices:            # per-device overrides
  node7: {user: root, password: ...}
  leaf9: {community: ...}
```

## Findings and fixes

| class | meaning | fix text |
|---|---|---|
| `wrong_port` | the NIC shows up on another switch port; the intended port is empty or shows something else | `move cable one port up` / `one port down` / `k ports up|down` (k ≤ 4, same switch) / `move cable from leaf4/30 to leaf4/21` |
| `missing_link` | intended port has no neighbor and the NIC is seen nowhere | `seat cable CBL-12-07-03 at leaf4/21`; `check NIC mlx5_3 on node7` only when the BMC reports the NIC port `LinkDown` (a cable is seated at the NIC but trains no link); `NoLink` means nothing is seated and keeps `seat cable` |
| `link_degraded` | right neighbor, but port down or below intended speed (a down port whose NIC is live elsewhere is a stale LLDP entry: that is a `wrong_port`) | check cable / transceivers |
| `unexpected_link` | a neighbor on a port with no cable in intent, not explained by a `wrong_port` (informational, does not change the exit code). Switch-to-switch cables in intent are expected at both ends; stale entries on down ports are notes | trace and update intent |
| `unreachable_node` | Redfish unreachable or HTTP error (also used for a switch whose LLDP table could not be read) | check reachability and credentials |
| `firmware_off_baseline` | one finding per component whose version differs from the baseline; unknown versions never fail. The running image is compared, not a standby/backup bank listed before it; baseline models match case-insensitively; with no baseline for any node the summary reads `firmware not compared` | `update bmc on node7 to 1.12.0` |
| `gpu_count_mismatch` | DCGM GPU count differs from the expected count (`gpu_count` in intent, else by model: NVL72 → 4, HGX → 8) | reseat / replace |
| `gpu_unhealthy` | per GPU: XID, ECC double-bit, row-remap failure, uncorrectable remapped rows | drain and open an RMA packet |
| `nvlink_degraded` | NVLink CRC FLIT errors above 100 or zero NVLink bandwidth with more than one GPU | check NVLink |
| `node_health_warning` | Redfish `Status.Health` is not OK | read the BMC event log |

A `wrong_port` is one finding, correlated across the NIC and both switch ports: the empty intended port and
the neighbor on the wrong port are explained by it, not reported twice. It is reported even when the
intended switch cannot be read. A port that shows the intended node but a port id nothing resolves (MAC ids,
no MACs in NetBox, BMC down) is "not verified" in `notes`, never "seat cable"; a switch that answers with an
empty table is unreachable, not "all unplugged". A `swap_pair` of two cables is two
`wrong_port` findings. Partial Redfish trees (no `FirmwareInventory`) and an unreachable DCGM exporter are
reported as unknown in `notes`, never as findings.

## The report

`<site>-<YYYY-MM-DD>.json` in the working directory (`--out` to choose; `--json` prints it to stdout and the
human layout to stderr):

```json
{"schema": "turnup.report/v1", "tool": "turnup-check", "tool_version": "0.1.0",
 "site": "hall-b", "simulated": false, "generated_at": "2026-10-16T18:00:04Z", "elapsed_s": 4.1,
 "intent": {"links": 288, "nodes": 18, "leaves": 2, "racks": 1, "source": "netbox"},
 "summary": {"links_ok": 287, "links_total": 288, "nodes_reachable": 18, "nodes_total": 18,
             "firmware_ok": true, "gpus_expected": 72, "gpus_seen": 72, "findings": 1},
 "findings": [{"id": "f-0001", "class": "wrong_port", "severity": "high",
               "cable": "CBL-12-07-03", "device": "node7", "interface": "mlx5_3",
               "expected": "leaf4/21", "observed": "leaf4/22",
               "diagnosis": "neighbor mismatch", "fix": "move cable one port up",
               "evidence": {"leaf4/21": {"link_up": false, "neighbor": null},
                            "leaf4/22": {"link_up": true, "speed_gbps": 400, "neighbor": "node7 mlx5_3"}}}],
 "sources": [{"kind": "lldp", "protocol": "nvue", "devices": 2}, {"kind": "redfish", "devices": 18},
             {"kind": "dcgm", "devices": 18}],
 "notes": []}
```

## Flags

`--site`, `--intent`, `--baseline`, `--out`, `--timeout` (default 3), `--json`, `--telemetry`,
`--only lldp,redfish,dcgm`, `--creds`, `-v` (print notes), `--version`.

## Opt-in telemetry

Nothing is sent unless you pass `--telemetry`. Then, after the report is written, one request goes to the
counter (`TURNUP_COUNTER_URL`, default `https://counter.legent.xyz/v1/ping`):

```json
{"schema": "turnup.ping/v1", "install_id": "<uuid4 from ~/.config/turnup/install_id>", "tool_version": "0.1.0",
 "simulated": false, "racks_checked": 1, "links_verified": 287, "nodes_checked": 18, "ts": "2026-10-16T18:00:04Z"}
```

That is the whole payload: no site names, device names, IPs or findings, ever. Sending has a 2 s timeout,
never raises and never changes the exit code. Runs against a simulated hall carry `simulated: true` and are never
counted. The counter publishes its numbers at the first update, once three operators have run the check on
real halls.

## Try it on the virtual hall

[`simhall`](https://github.com/turnup-dev/simhall) (a separate repository, Apache-2.0) is a virtual hall
that serves the same NetBox, Redfish, eAPI/NVUE and DCGM protocols with fault injection. The golden scenario
reproduces the output at the top of this page; run the server in one terminal and the check in another (no
token is needed, simhall accepts any):

```bash
pip install simhall                 # or: pip install git+https://github.com/turnup-dev/simhall
simhall serve --golden hall-b
turnup-check --site hall-b --intent http://127.0.0.1:8900/netbox
```

That exits 1 with the one `wrong_port` finding. A technician seats the cable (or the fault is cleared), and
the same command exits 0 with `288 / 288`:

```bash
simhall act '{"kind":"seat_cable","label":"CBL-12-07-03","b_interface":"swp21"}'   # or: simhall faults --clear flt-0001
turnup-check --site hall-b --intent http://127.0.0.1:8900/netbox
```

## NetBox plugin

`netbox_plugin/` holds `netbox-turnup-check`, a plugin that adds a **Turnup check** menu item listing the
report files from a directory. See `netbox_plugin/README.md` and `netbox_plugin/NAUTOBOT.md`.

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
`git+https://github.com/turnup-dev/simhall@main` and runs the whole suite, integration tier included. The
integration job runs only while this repository is public, because the workflow token cannot read a private
sibling repository; on a private repository it shows as skipped.

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

## Clean room

Written from public specifications only: DMTF Redfish, IEEE 802.1AB LLDP and LLDP-MIB, NVIDIA DCGM-exporter
metric names, NetBox and Nautobot REST API docs, Arista eAPI and NVIDIA NVUE public docs, the Prometheus
exposition format. Example halls are synthetic.

## License

Apache-2.0. See `LICENSE`.
