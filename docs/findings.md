# Findings and fixes

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
