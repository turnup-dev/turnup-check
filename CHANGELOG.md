# Changelog

All notable changes to turnup-check. The format follows Keep a Changelog; versions follow SemVer.

## [Unreleased]

## [0.1.0] - 2026-10-25

First public release (v0). Everything below is written against public specifications and tested against the
`simhall` virtual hall; no real hall has run it yet.

### Added

- Intent adapters: NetBox 3.x/4.x REST (pagination, `role.slug` / `device_role.slug`, custom fields
  `turnup_bmc_url` / `turnup_dcgm_url` / `turnup_switch_url`, `config_context.turnup.firmware_baseline`,
  `a_terminations` and legacy `termination_a`, both MAC field shapes), Nautobot (`location=`, `role.name`,
  `?depth=`), YAML/JSON files, and bare NetBox base URLs.
- Collectors over asyncio + httpx with a 64-request semaphore, a per-host limit of 4 (`TURNUP_PER_HOST`) and
  per-request timeouts: DMTF Redfish (Systems, Managers, FirmwareInventory with component matching,
  Chassis/NetworkAdapters/Ports with LLDPReceive fetched in a second pass for nodes with link findings; partial
  trees become "unknown"), LLDP via Arista eAPI, NVIDIA NVUE and SNMP LLDP-MIB (pysnmp, optional), and the
  DCGM-exporter text parser.
- Diff engine with the ten finding classes of the interface contract, `wrong_port` correlation across the NIC
  and both switch ports, the fix sentences (`move cable one port up`, `k ports up/down`, `move cable from ... to ...`,
  `seat cable ... at ...`, `check NIC ... on ...`), and link / node / firmware / GPU summaries.
- `turnup.report/v1` JSON writer, the fixed CLI layout, exit codes 0/1/2, `--json`, `--out`, `--only`, `--creds`,
  `--baseline`, `--timeout`.
- Opt-in telemetry (`--telemetry`): one `turnup.ping/v1` with counts only, install id under `~/.config/turnup/`,
  default counter `https://counter.legent.xyz/v1/ping`, never blocks or changes the exit code; simulated
  halls are flagged.
- Tests: unit tier with canned eAPI, NVUE, Redfish, DCGM, NetBox, Nautobot and SNMP fixtures; CLI tests with a
  fake collector; integration tier (golden hall-b, hypothesis property test, chaos, scale) against simhall.
- `netbox_plugin/`: skeleton NetBox plugin `netbox-turnup-check` (menu item + report list/detail views) and a
  Nautobot app note.
- GitHub Actions workflows: ruff and pytest on Python 3.12 and 3.13 (`ci.yml`; the integration tier runs once
  the repository is public), and PyPI publishing on `v*` tags through trusted publishing (`release.yml`).
- From the demo run (D4): each unrecognised device role is listed once in `notes` with its device count; the
  role mapping itself is unchanged (`core-switch`, `router`, patch panels and generic servers stay `other`, a
  product call).
- `NetBoxIntentSource(page_limit=)` (default 1000), `Intent.notes` (not serialized), `INTENT_TIMEOUT`.
- Tests over the captured demo pages (`tests/fixtures/netbox_demo/`, `tests/fixtures/nautobot_demo/`,
  `tests/test_intent_demo_fixtures.py`): device, interface and cable counts per site and one test per fix.

### Fixed (before the first release, found by the integration tier against simhall)

- The two summary lines always carry the check mark (`✓ 287 / 288 links match intent`), as the interface
  contract and the README example print them; the cross marks each finding block. Previously the links line
  switched to a cross whenever a link was off.
- `missing_link` fix: `check NIC <if> on <node>` only when the BMC reports the NIC port `LinkDown`; `NoLink`
  (nothing seated at the NIC, what a simulated unseated cable shows) keeps `seat cable <label> at <S>/<P>`.
  The NIC evidence now carries `link_status`.
- NVUE: a down port's `link.speed` is its configured speed, not a negotiated one, so down ports report no
  speed (the empty intended port's evidence is `{"link_up": false, "neighbor": null}`, as in the contract).

### Fixed (before the first release, found against the public NetBox and Nautobot demos)

Found on 2026-10-16 by running the intent adapters against the public NetBox 4.7.3 and Nautobot 3.2.7 demos;
D1-D10 number the defects of that run.

- D10: reading intent from Nautobot timed out (`reading intent failed: nautobot: ReadTimeout`, exit 2) because
  the cable list at `depth=2` takes 80-177 s server-side, the adapter used a fixed 10 s timeout, and `--timeout`
  was never passed to intent loading. Cables are now read at `depth=1` and their ends resolved against the
  device list; intent reads use `max(--timeout, 120)` s per request (`INTENT_TIMEOUT`) with a 10 s connect cap.
- D1: Nautobot devices are read at `depth=2`; at `depth=1` `device_type.manufacturer` was a bare reference, so
  every switch fell back to SNMP (the demo's Arista leaves now resolve to eAPI) and the `primary_ip4` mgmt
  fallback never fired.
- D2: Nautobot devices are read with `include=config_context`; list views omit it, so the
  `config_context.turnup.firmware_baseline` path was dead.
- D6: a skipped cable's note names both ends' object types (`not interface-to-interface (a=dcim.powerport,
  b=dcim.poweroutlet)`), and adapter notes now reach the report's `notes` (they were dropped before).
- D8: `virtual`, `lag` and `bridge` interfaces are left out of intent, with one note counting them.
- D9: `nvlink-switch` devices get `mgmt.switch_url` (custom field or `primary_ip4`) and `mgmt.protocol` like
  leaf and spine switches; they are still not polled as fabric switches.

Not changed: NetBox auth stays `Authorization: Token` (D3 refuted: NetBox 4.7.3 accepts v2 tokens under it);
multi-termination cables (D5) did not occur on the demos; the Nautobot `location=` filter (D7) was honoured on
the demos and needed no change.

[Unreleased]: https://github.com/turnup-dev/turnup-check/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/turnup-dev/turnup-check/releases/tag/v0.1.0
