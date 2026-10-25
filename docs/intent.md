# Intent: NetBox, Nautobot, a URL or a file

The intended build is read from NetBox or Nautobot, from a NetBox-shaped URL, or from a `turnup.intent/v1` file. This page lists exactly what is read and how.

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
