# The report and the flags

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
