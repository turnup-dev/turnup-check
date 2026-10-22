# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Report JSON (turnup.report/v1) and the exact CLI layout of CONTRACT.md section 4.

Expected lines are written with ASCII stand-ins ([ok] [x] [.] ->) and converted by ``G`` so the
test source stays ASCII while the assertions are byte-exact against the glyph output.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

from helpers import G, load_text

from turnup_check import __version__
from turnup_check.collectors import poll_all
from turnup_check.diff import diff
from turnup_check.report import (
    build_report,
    emit,
    intent_line,
    polling_line,
    render_lines,
    report_filename,
    to_ascii,
    write_report,
)

GOLDEN = "CBL-12-07-03"
WHEN = datetime(2026, 10, 16, 18, 0, 4, tzinfo=UTC)


async def golden_like(mini_intent, fake):
    fake.move_cable(mini_intent, GOLDEN, "leaf4", "swp22")
    obs = await poll_all(mini_intent, fake, timeout=1.0)
    result = diff(mini_intent, obs)
    return build_report(mini_intent, obs, result, elapsed_s=4.1, generated_at=WHEN)


async def test_report_json_shape(mini_intent, fake):
    report = await golden_like(mini_intent, fake)
    doc = json.loads(json.dumps(report.as_json()))
    assert doc["schema"] == "turnup.report/v1"
    assert doc["tool"] == "turnup-check" and doc["tool_version"] == __version__
    assert doc["site"] == "hall-mini" and doc["simulated"] is True
    assert doc["generated_at"] == "2026-10-16T18:00:04Z" and doc["elapsed_s"] == 4.1
    assert doc["intent"] == {"links": 8, "nodes": 2, "leaves": 2, "racks": 1, "source": "file"}
    assert doc["summary"] == {
        "links_ok": 7,
        "links_total": 8,
        "nodes_reachable": 2,
        "nodes_total": 2,
        "firmware_ok": True,
        "gpus_expected": 8,
        "gpus_seen": 8,
        "findings": 1,
    }
    (f,) = doc["findings"]
    assert f == {
        "id": "f-0001",
        "class": "wrong_port",
        "severity": "high",
        "cable": "CBL-12-07-03",
        "device": "node7",
        "interface": "mlx5_3",
        "expected": "leaf4/21",
        "observed": "leaf4/22",
        "diagnosis": "neighbor mismatch",
        "fix": "move cable one port up",
        "evidence": {
            "leaf4/21": {"link_up": False, "neighbor": None},
            "leaf4/22": {"link_up": True, "speed_gbps": 400, "neighbor": "node7 mlx5_3"},
            "node7 mlx5_3": {
                "link_up": True,
                "link_status": "LinkUp",
                "speed_gbps": 400.0,
                "lldp_receive": "leaf4/22",
            },
        },
    }
    assert doc["sources"] == [
        {"kind": "lldp", "protocol": "nvue", "devices": 2},
        {"kind": "redfish", "devices": 2},
        {"kind": "dcgm", "devices": 2},
    ]
    assert doc["notes"] == []
    assert report.exit_code == 1


def test_report_filename_uses_utc_date():
    assert report_filename("hall-b", WHEN) == "hall-b-2026-10-16.json"
    late = datetime(2026, 10, 16, 23, 30, tzinfo=UTC)
    assert report_filename("hall-b", late) == "hall-b-2026-10-16.json"


async def test_write_report_default_and_override(tmp_path, mini_intent, fake):
    report = await golden_like(mini_intent, fake)
    path = write_report(report, cwd=tmp_path)
    assert path == tmp_path / "hall-mini-2026-10-16.json"
    assert json.loads(path.read_text())["summary"]["findings"] == 1
    custom = write_report(report, tmp_path / "out" / "r.json")
    assert custom.exists() and custom.name == "r.json"


async def test_exact_cli_layout_for_the_golden_run(mini_intent, fake):
    report = await golden_like(mini_intent, fake)
    lines = render_lines(report, report_path="hall-mini-2026-10-16.json", intent=mini_intent)
    assert lines == [
        G("reading intent      8 links [.] 2 nodes [.] 2 leaves"),
        G("polling             lldp [.] redfish [.] dcgm"),
        G("[ok] 7 / 8 links match intent"),
        G("[ok] 2 / 2 nodes reachable, firmware baseline ok"),
        G("[x] mismatch   node7 mlx5_3 -> leaf4/22"),
        "             expected leaf4/21",
        "fix          move cable one port up",
        "elapsed      4.1 s",
        "",
        "report       hall-mini-2026-10-16.json",
        "share stats  --telemetry opt-in (racks, links)",
    ]
    # Column widths of the contract: 20 for the two header lines, 13 for the rest.
    assert lines[0].index("8 links") == 20 and lines[4].index("node7") == 13 and lines[6].index("move") == 13


async def test_layout_all_green(mini_intent, fake):
    obs = await poll_all(mini_intent, fake, timeout=1.0)
    report = build_report(mini_intent, obs, diff(mini_intent, obs), elapsed_s=0.26, generated_at=WHEN)
    lines = render_lines(report, report_path="x.json", telemetry="sent (1 rack, 8 links)")
    assert lines == [
        G("[ok] 8 / 8 links match intent"),
        G("[ok] 2 / 2 nodes reachable, firmware baseline ok"),
        "elapsed      0.3 s",
        "",
        "report       x.json",
        "share stats  sent (1 rack, 8 links)",
    ]
    assert report.exit_code == 0


async def test_layout_for_every_finding_class(mini_intent, fake):
    fake.unseat(mini_intent, "CBL-12-08-02")
    fake.degrade(mini_intent, "CBL-12-07-00", speed_gbps=100)
    fake.node_unreachable("node8", "timeout after 3 s")
    fake.node_firmware("node7", "bios", "2.2.0")
    fake.node_health("node7", "Warning")
    fake.node_dcgm("node7", load_text("dcgm/xid.txt"))
    fake.add_neighbor("leaf3", "swp6", "camera", "eth0")
    obs = await poll_all(mini_intent, fake, timeout=1.0)
    report = build_report(mini_intent, obs, diff(mini_intent, obs), elapsed_s=1.0, generated_at=WHEN)
    text = "\n".join(render_lines(report, report_path="x.json"))
    # Summary lines always carry the check mark (README / CONTRACT section 4); findings carry the cross.
    assert G("[ok] 6 / 8 links match intent") in text
    assert G("[ok] 1 / 2 nodes reachable, firmware off baseline") in text
    assert G("[x] degraded   node7 mlx5_0 -> leaf3/1 100G\n             expected leaf3/1 up 400G") in text
    # node8's BMC is unreachable here, so the NIC state is unknown and the fix is to seat the cable.
    assert (
        G("[x] missing    node8 mlx5_2 -> not seen\n             expected leaf3/4\n")
        + "fix          seat cable CBL-12-08-02 at leaf3/4"
    ) in text
    assert G("[x] offline    node8 redfish unreachable: timeout after 3 s") in text
    assert G("[x] health     node7 health Warning\n             expected OK") in text
    assert (
        G("[x] firmware   node7 bios 2.2.0\n             expected 2.3.1\n")
        + "fix          update bios on node7 to 2.3.1"
    ) in text
    assert G("[x] gpu        node7 gpu2 XID 79\n             expected healthy") in text
    assert G("[.] unexpected leaf3/swp6 shows camera eth0") in text


async def test_emit_plain_and_ascii_fallback(mini_intent, fake):
    report = await golden_like(mini_intent, fake)
    lines = render_lines(report, report_path="r.json")
    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    emit(lines, utf8, color=False)
    utf8.seek(0)
    assert G("[x] mismatch   node7 mlx5_3 -> leaf4/22") in utf8.read()
    ascii_stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    emit(lines, ascii_stream, color=False)
    ascii_stream.seek(0)
    out = ascii_stream.read()
    assert "!! mismatch   node7 mlx5_3 -> leaf4/22" in out and "ok 2 / 2 nodes" in out
    assert to_ascii(G("a [.] b")) == "a - b"


async def test_emit_with_rich_color_keeps_text(mini_intent, fake):
    report = await golden_like(mini_intent, fake)
    buf = io.StringIO()
    emit(render_lines(report, report_path="r.json", intent=mini_intent), buf, color=True)
    out = buf.getvalue()
    assert "move cable one port up" in out and "reading intent" in out


def test_header_lines(mini_intent):
    assert intent_line(mini_intent) == G("reading intent      8 links [.] 2 nodes [.] 2 leaves")
    assert polling_line(["lldp"]) == "polling             lldp"
