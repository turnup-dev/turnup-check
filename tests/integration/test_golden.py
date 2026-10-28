# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Golden tier (CONTRACT section 8): the README run against ``simhall serve --golden hall-b`` over HTTP."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

pytest.importorskip("simhall", reason="simhall not installed in this venv", exc_type=ImportError)

from helpers import CounterStub
from simhall_helpers import ARROW, CHECK, CROSS, DOT, SimServer, run_cli

pytestmark = pytest.mark.integration

GOLDEN_LABEL = "CBL-12-07-03"
REPORT_KEYS = {
    "schema",
    "tool",
    "tool_version",
    "site",
    "simulated",
    "generated_at",
    "elapsed_s",
    "intent",
    "summary",
    "findings",
    "sources",
    "notes",
}
FINDING_KEYS = {
    "id",
    "class",
    "severity",
    "cable",
    "device",
    "interface",
    "expected",
    "observed",
    "diagnosis",
    "fix",
    "evidence",
}


def _elapsed(output: str) -> float:
    m = re.search(r"^elapsed\s+([\d.]+) s$", output, re.M)
    assert m, output
    return float(m.group(1))


def test_golden_hall_b_over_netbox_adapter(golden: SimServer, runner_env, tmp_path: Path):
    res = run_cli(["--site", "hall-b", "--intent", golden.netbox_url], runner_env, tmp_path)
    out = res.stdout
    assert res.returncode == 1, (out, res.stderr, golden.log())
    lines = out.splitlines()
    # The README output, line for line: the check marks the summary lines, the cross marks each finding block.
    # Five findings of four kinds, one node each, one fix sentence each (simhall's golden scenario).
    assert lines[0] == f"reading intent      288 links {DOT} 18 nodes {DOT} 2 leaves"
    assert lines[1] == f"polling             lldp {DOT} redfish {DOT} dcgm"
    assert lines[2] == f"{CHECK} 286 / 288 links match intent"
    assert lines[3] == f"{CHECK} 17 / 18 nodes reachable, firmware off baseline"
    assert lines[4] == f"{CROSS} mismatch   node7 mlx5_3 {ARROW} leaf4/22"
    assert lines[5] == "             expected leaf4/21"
    assert lines[6] == "fix          move cable one port up"
    assert lines[7] == f"{CROSS} degraded   node11 mlx5_5 {ARROW} leaf4/83 200G"
    assert lines[8] == "             expected leaf4/83 up 400G"
    assert lines[9] == "fix          check transceivers and cable CBL-12-11-05 at leaf4/83 (200G negotiated, intent 400G)"
    assert lines[10] == f"{CROSS} firmware   node4 bmc 1.09.2"
    assert lines[11] == "             expected 1.12.0"
    assert lines[12] == "fix          update bmc on node4 to 1.12.0"
    assert lines[13] == f"{CROSS} gpu        node14 gpu2 XID 79"
    assert lines[14] == "             expected healthy"
    assert lines[15] == "fix          drain node14 and open an RMA packet for GPU 2"
    assert lines[16] == f"{CROSS} offline    node17 redfish unreachable: timeout after 3 s"
    assert lines[17] == "fix          check BMC reachability and credentials for node17"
    assert lines[18].startswith("elapsed      ") and lines[18].endswith(" s")
    assert lines[19] == ""
    assert lines[20].startswith("report       hall-b-") and lines[20].endswith(".json")
    assert lines[21] == "share stats  --telemetry opt-in (racks, links)"
    assert len(lines) == 22
    assert _elapsed(out) < 6.0  # node17's BMC costs one 3 s timeout

    report_path = tmp_path / lines[20].split()[-1]
    doc = json.loads(report_path.read_text())
    assert set(doc) == REPORT_KEYS
    assert doc["schema"] == "turnup.report/v1" and doc["tool"] == "turnup-check"
    assert re.match(r"^\d+\.\d+\.\d+", doc["tool_version"])
    assert doc["site"] == "hall-b" and doc["simulated"] is True  # inferred from the NetBox site description
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", doc["generated_at"])
    assert 0 < doc["elapsed_s"] < 6.0
    assert doc["intent"] == {"links": 288, "nodes": 18, "leaves": 2, "racks": 1, "source": "netbox"}
    assert doc["summary"] == {
        "links_ok": 286,
        "links_total": 288,
        "nodes_reachable": 17,
        "nodes_total": 18,
        "firmware_ok": False,
        "gpus_expected": 72,
        "gpus_seen": 68,  # node17's exporter is unreachable too: its four GPUs are unknown, not missing
        "findings": 5,
    }
    assert [f["class"] for f in doc["findings"]] == [
        "wrong_port", "link_degraded", "firmware_off_baseline", "gpu_unhealthy", "unreachable_node"
    ]
    assert [f["device"] for f in doc["findings"]] == ["node7", "node11", "node4", "node14", "node17"]
    assert [f["severity"] for f in doc["findings"]] == ["high", "medium", "medium", "high", "high"]
    assert all(set(f) == FINDING_KEYS for f in doc["findings"])
    finding = doc["findings"][0]
    assert finding["id"] == "f-0001" and finding["class"] == "wrong_port" and finding["severity"] == "high"
    assert (
        finding["cable"] == GOLDEN_LABEL and finding["device"] == "node7" and finding["interface"] == "mlx5_3"
    )
    assert finding["expected"] == "leaf4/21" and finding["observed"] == "leaf4/22"
    assert finding["diagnosis"] == "neighbor mismatch" and finding["fix"] == "move cable one port up"
    # Evidence exactly as the contract example shows it, plus the NIC's own view from the second Redfish pass.
    assert finding["evidence"]["leaf4/21"] == {"link_up": False, "neighbor": None}
    assert finding["evidence"]["leaf4/22"] == {"link_up": True, "speed_gbps": 400, "neighbor": "node7 mlx5_3"}
    assert finding["evidence"]["node7 mlx5_3"]["link_up"] is True
    assert finding["evidence"]["node7 mlx5_3"]["lldp_receive"] == "leaf4/22"
    assert doc["sources"] == [
        {"kind": "lldp", "protocol": "nvue", "devices": 2},
        {"kind": "redfish", "devices": 18},
        {"kind": "dcgm", "devices": 18},
    ]
    assert doc["notes"] == ["node17: dcgm unreachable (timeout after 3 s); gpu: unknown"]


def test_seating_the_cable_closes_the_mismatch_and_leaves_four(golden: SimServer, runner_env, tmp_path: Path):
    golden.act(
        "seat_cable",
        label=GOLDEN_LABEL,
        a_device="node7",
        a_interface="mlx5_3",
        b_device="leaf4",
        b_interface="swp21",
    )
    res = run_cli(["--site", "hall-b", "--intent", golden.netbox_url], runner_env, tmp_path)
    assert res.returncode == 1, (res.stdout, res.stderr, golden.log())
    lines = res.stdout.splitlines()
    assert lines[2] == f"{CHECK} 287 / 288 links match intent"
    assert lines[3] == f"{CHECK} 17 / 18 nodes reachable, firmware off baseline"
    assert "mismatch" not in res.stdout and res.stdout.count(CROSS) == 4


def test_the_floor_actions_leave_the_two_node_findings_open(golden: SimServer, runner_env, tmp_path: Path):
    """The demo's closing re-run: cable moved, degraded cable reseated, firmware updated; the GPU with the
    XID and the BMC that does not answer stay open, so the exit code stays 1 (no clean board)."""
    golden.act("seat_cable", label=GOLDEN_LABEL, b_interface="swp21")
    golden.act("seat_cable", label="CBL-12-11-05")
    golden.act("update_firmware", device="node4", component="bmc", version="1.12.0")
    res = run_cli(["--site", "hall-b", "--intent", golden.netbox_url, "--json"], runner_env, tmp_path)
    assert res.returncode == 1, (res.stdout, res.stderr, golden.log())
    doc = json.loads(res.stdout)
    assert doc["summary"] == {
        "links_ok": 288,
        "links_total": 288,
        "nodes_reachable": 17,
        "nodes_total": 18,
        "firmware_ok": True,
        "gpus_expected": 72,
        "gpus_seen": 68,
        "findings": 2,
    }
    assert [(f["class"], f["device"]) for f in doc["findings"]] == [
        ("gpu_unhealthy", "node14"),
        ("unreachable_node", "node17"),
    ]
    assert f"{CHECK} 288 / 288 links match intent" in res.stderr
    assert f"{CHECK} 17 / 18 nodes reachable, firmware baseline ok" in res.stderr


def test_clearing_the_faults_makes_the_run_green(golden: SimServer, runner_env, tmp_path: Path):
    faults = golden.faults()
    assert [f["kind"] for f in faults] == [
        "wrong_port", "link_degraded", "firmware_off_baseline", "gpu_unhealthy", "unreachable_node"
    ], faults
    for f in faults:
        golden.clear(str(f.get("id")))
    res = run_cli(["--site", "hall-b", "--intent", golden.netbox_url, "--json"], runner_env, tmp_path)
    assert res.returncode == 0, (res.stdout, res.stderr)
    doc = json.loads(res.stdout)
    assert doc["summary"] == {
        "links_ok": 288,
        "links_total": 288,
        "nodes_reachable": 18,
        "nodes_total": 18,
        "firmware_ok": True,
        "gpus_expected": 72,
        "gpus_seen": 72,
        "findings": 0,
    }
    assert doc["findings"] == [] and doc["notes"] == []
    assert f"{CHECK} 288 / 288 links match intent" in res.stderr  # human layout goes to stderr with --json


def test_same_result_from_the_intent_file(golden: SimServer, runner_env, tmp_path: Path):
    intent_path = tmp_path / "hall-b.json"
    intent_path.write_text(json.dumps(golden.intent()), encoding="utf-8")
    res = run_cli(["--intent", str(intent_path), "--out", str(tmp_path / "r.json")], runner_env, tmp_path)
    assert res.returncode == 1, (res.stdout, res.stderr)
    assert f"{CROSS} mismatch   node7 mlx5_3 {ARROW} leaf4/22" in res.stdout
    assert "fix          move cable one port up" in res.stdout
    doc = json.loads((tmp_path / "r.json").read_text())
    assert doc["intent"]["source"] == "file" and doc["simulated"] is True
    assert doc["summary"]["links_ok"] == 286 and doc["summary"]["findings"] == 5


def test_telemetry_ping_from_the_golden_run(golden: SimServer, runner_env, tmp_path: Path):
    """``--telemetry`` after the report: one POST with exactly the eight fields, ``simulated: true`` because
    the NetBox site says "Simulated by simhall"; the finding, not the ping, sets the exit code."""
    with CounterStub() as stub:
        env = dict(runner_env, TURNUP_COUNTER_URL=stub.url)
        res = run_cli(["--site", "hall-b", "--intent", golden.netbox_url, "--telemetry"], env, tmp_path)
    assert res.returncode == 1, (res.stdout, res.stderr)
    assert res.stdout.splitlines()[-1] == "share stats  sent (1 rack, 286 links)"
    ((path, headers, body),) = stub.received
    assert path == "/v1/ping" and headers["content-type"].startswith("application/json")
    assert set(body) == {
        "schema",
        "install_id",
        "tool_version",
        "simulated",
        "racks_checked",
        "links_verified",
        "nodes_checked",
        "ts",
    }
    assert body["schema"] == "turnup.ping/v1" and body["simulated"] is True
    assert (body["racks_checked"], body["links_verified"], body["nodes_checked"]) == (1, 286, 18)
    assert uuid.UUID(body["install_id"]).version == 4
    assert body["install_id"] == (tmp_path / "cfg" / "install_id").read_text().strip()
    assert re.match(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$", body["tool_version"])
    assert body["ts"].endswith("Z")
    for forbidden in ("hall-b", "node7", "leaf4", "127.0.0.1", "mlx5", "wrong_port", GOLDEN_LABEL):
        assert forbidden not in json.dumps(body)
