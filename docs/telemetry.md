# Opt-in telemetry

Nothing is sent unless you pass `--telemetry`. Then, after the report is written, one request goes to the
counter (`TURNUP_COUNTER_URL`, default `https://counter.legent.xyz/v1/ping`):

```json
{"schema": "turnup.ping/v1", "install_id": "<uuid4 from ~/.config/turnup/install_id>", "tool_version": "0.1.0",
 "simulated": false, "racks_checked": 1, "links_verified": 287, "nodes_checked": 18, "ts": "2026-10-16T18:00:04Z"}
```

That is the whole payload: no site names, device names, IPs or findings, ever. Sending has a 2 s timeout,
never raises and never changes the exit code. Runs against a simulated hall carry `simulated: true` and are never
counted. The counter keeps aggregate totals only; nothing is published from it until there is something worth showing.
