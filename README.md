# turnup-check

Acceptance check for GPU halls: reads the intended build from NetBox or Nautobot, polls the hall over
LLDP, Redfish and DCGM, and prints the diff. One command, seconds per rack, any vendor, Apache-2.0.

```
$ turnup-check --site hall-b --intent netbox
reading intent      288 links · 18 nodes · 2 leaves
polling             lldp · redfish · dcgm
✓ 286 / 288 links match intent
✓ 17 / 18 nodes reachable, firmware off baseline
✗ mismatch   node7 mlx5_3 → leaf4/22
             expected leaf4/21
fix          move cable one port up
✗ degraded   node11 mlx5_5 → leaf4/83 200G
             expected leaf4/83 up 400G
fix          check transceivers and cable CBL-12-11-05 at leaf4/83 (200G negotiated, intent 400G)
✗ firmware   node4 bmc 1.09.2
             expected 1.12.0
fix          update bmc on node4 to 1.12.0
✗ gpu        node14 gpu2 XID 79
             expected healthy
fix          drain node14 and open an RMA packet for GPU 2
✗ offline    node17 redfish unreachable: timeout after 3 s
fix          check BMC reachability and credentials for node17
elapsed      4.1 s

report       hall-b-<YYYY-MM-DD>.json
share stats  --telemetry opt-in (racks, links)
```

Every finding comes with the fix a technician can act on. Exit code `0` means the hall matches intent,
`1` means findings, `2` means the intent could not be read or no telemetry source answered. It writes one
JSON report. Nothing else: no agent, no database, no account.

## Install

```bash
pip install turnup-check            # Python 3.12+
pip install "turnup-check[snmp]"    # adds pysnmp for switches without eAPI/NVUE
```

## Five minutes on a NetBox site

```bash
export NETBOX_URL=https://netbox.example NETBOX_TOKEN=...
export TURNUP_SWITCH_USER=admin TURNUP_SWITCH_PASS=...     # LLDP from the leaves
export TURNUP_BMC_USER=admin TURNUP_BMC_PASS=...           # Redfish from the nodes
turnup-check --site <slug> --intent netbox
```

`--only lldp` is the fastest first run: cabling only, switch credentials only. A device that refuses or does
not answer is reported as unreachable, never fatal. Nautobot works the same way with `--intent nautobot`;
so does a `turnup.intent/v1` YAML file. What is read from NetBox, the device roles it expects, the
management-endpoint custom fields and the firmware baseline are in [docs/intent.md](https://github.com/turnup-dev/turnup-check/blob/main/docs/intent.md).

## Try it without hardware

[simhall](https://github.com/turnup-dev/simhall) is a virtual hall that serves the same NetBox, Redfish,
eAPI/NVUE and DCGM protocols with fault injection. Its golden scenario `hall-b` is a hall handed over with
every light green and five faults of four kinds underneath; it reproduces the output above:

```bash
pip install simhall
simhall serve --golden hall-b &                                      # 127.0.0.1:8900, any token
turnup-check --site hall-b --intent http://127.0.0.1:8900/netbox    # exits 1: five findings
simhall act '{"kind":"seat_cable","label":"CBL-12-07-03","b_interface":"swp21"}'      # the cable moved
simhall act '{"kind":"seat_cable","label":"CBL-12-11-05"}'                             # degraded cable reseated
simhall act '{"kind":"update_firmware","device":"node4","component":"bmc","version":"1.12.0"}'
turnup-check --site hall-b --intent http://127.0.0.1:8900/netbox    # exits 1: 288 / 288, the GPU and node17 still open
simhall act '{"kind":"replace_node","device":"node14","new_serial":"SN-R1"}'          # the RMA swap ...
simhall act '{"kind":"replace_node","device":"node17","new_serial":"SN-R2"}'          # ... and the dead BMC's tray
turnup-check --site hall-b --intent http://127.0.0.1:8900/netbox    # exits 0
```

Simulated runs are marked `simulated: true` in the report and never count anywhere.

## What it finds

| Finding | Fix it prints |
|---|---|
| `wrong_port`: the NIC shows up on another switch port | `move cable one port up` (or `k ports down`, or `from leaf4/30 to leaf4/21`) |
| `missing_link`: intended port empty, NIC seen nowhere | `seat cable CBL-12-07-03 at leaf4/21`; `check NIC` when the BMC says the NIC port is down |
| `link_degraded`: right neighbor, port down or below intended speed | check cable / transceivers |
| `unexpected_link`: a neighbor where intent has no cable | trace and update intent |
| `unreachable_node`: Redfish down or HTTP error | check reachability and credentials |
| `firmware_off_baseline`: a component differs from the baseline | `update bmc on node7 to 1.12.0` |
| `gpu_count_mismatch`, `gpu_unhealthy`, `nvlink_degraded`, `node_health_warning` | reseat, RMA packet, check NVLink, read the BMC log |

A `wrong_port` is one finding, correlated across the NIC and both switch ports, never two. The exact rules,
including what is a note rather than a finding, are in [docs/findings.md](https://github.com/turnup-dev/turnup-check/blob/main/docs/findings.md).

## How it works

Switches are read over Arista eAPI, NVIDIA NVUE or SNMP (LLDP-MIB); nodes over DMTF Redfish and the DCGM
exporter. All devices are polled concurrently with a per-request deadline (`--timeout`, default 3 s), so a
dead device never stalls the run; a clean hall costs about ten Redfish requests per node.
[docs/polling.md](https://github.com/turnup-dev/turnup-check/blob/main/docs/polling.md) has the protocols, concurrency limits and the credentials file.

The report is `turnup.report/v1` JSON with the summary, every finding with its evidence, and the sources
that answered: [docs/report.md](https://github.com/turnup-dev/turnup-check/blob/main/docs/report.md). Telemetry is off unless you pass `--telemetry`; the payload
is counts only, never names: [docs/telemetry.md](https://github.com/turnup-dev/turnup-check/blob/main/docs/telemetry.md).

`netbox_plugin/` holds `netbox-turnup-check`, a NetBox plugin that lists report files in a menu
(`netbox_plugin/README.md`).

## Status

v0.1.0. The NetBox and Nautobot adapters have run against the public demos (NetBox 4.7, Nautobot 3.2),
not yet against a production GPU hall. The NVUE and SNMP paths follow the public docs and canned tables and
have not been verified on a real switch. The full list is in
[docs/development.md](https://github.com/turnup-dev/turnup-check/blob/main/docs/development.md#status-and-known-gaps-v010), with how to develop, test and
release.

Written from public specifications only: DMTF Redfish, IEEE 802.1AB LLDP and LLDP-MIB, NVIDIA
DCGM-exporter metric names, NetBox and Nautobot REST API docs, Arista eAPI and NVIDIA NVUE public docs,
the Prometheus exposition format. Example halls are synthetic.

## License

Apache-2.0. See `LICENSE`.
