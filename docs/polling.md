# What is polled

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
