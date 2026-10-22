# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""The turnup-check command with a file intent and a fake collector: layout, exit codes, flags."""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner
from helpers import FakeCollector, G

from turnup_check import cli
from turnup_check.intent.file import load_intent_file

GOLDEN = "CBL-12-07-03"


@pytest.fixture
def workdir(tmp_path, monkeypatch, mini_intent_path):
    """Isolated cwd and config dir: reports and install ids never land in the repo or the real home."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TURNUP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TURNUP_COUNTER_URL", raising=False)
    shutil.copy(mini_intent_path, tmp_path / "hall-mini.yaml")
    return tmp_path


@pytest.fixture
def fake_hall(monkeypatch, workdir):
    """Install a fake collector as the CLI's collector factory and return it for mutation."""
    intent = load_intent_file(workdir / "hall-mini.yaml")
    fake = FakeCollector.from_intent(intent)
    monkeypatch.setattr(cli, "make_collector", lambda: fake)
    return intent, fake


def invoke(*args: str):
    return CliRunner().invoke(cli.main, list(args), catch_exceptions=False)


def test_findings_exit_1_with_exact_layout(fake_hall, workdir):
    intent, fake = fake_hall
    fake.move_cable(intent, GOLDEN, "leaf4", "swp22")
    res = invoke("--site", "hall-mini", "--intent", "hall-mini.yaml")
    assert res.exit_code == 1, res.output
    lines = res.output.splitlines()
    assert lines[0] == G("reading intent      8 links [.] 2 nodes [.] 2 leaves")
    assert lines[1] == G("polling             lldp [.] redfish [.] dcgm")
    assert lines[2] == G("[ok] 7 / 8 links match intent")  # the check marks the summary, the cross a finding
    assert lines[3] == G("[ok] 2 / 2 nodes reachable, firmware baseline ok")
    assert lines[4] == G("[x] mismatch   node7 mlx5_3 -> leaf4/22")
    assert lines[5] == "             expected leaf4/21"
    assert lines[6] == "fix          move cable one port up"
    assert lines[7].startswith("elapsed      ") and lines[7].endswith(" s")
    assert lines[8] == ""
    assert lines[9].startswith("report       hall-mini-") and lines[9].endswith(".json")
    assert lines[10] == "share stats  --telemetry opt-in (racks, links)"
    report_name = lines[9].split()[-1]
    doc = json.loads((workdir / report_name).read_text())
    assert doc["findings"][0]["fix"] == "move cable one port up"
    assert doc["simulated"] is True and doc["intent"]["source"] == "file"


def test_clean_hall_exits_0(fake_hall):
    res = invoke("--intent", "hall-mini.yaml")
    assert res.exit_code == 0, res.output
    assert G("[ok] 8 / 8 links match intent") in res.output
    assert G("[ok] 2 / 2 nodes reachable, firmware baseline ok") in res.output
    assert "fix" not in res.output


def test_fix_then_rerun_exits_0(fake_hall):
    intent, fake = fake_hall
    fake.move_cable(intent, GOLDEN, "leaf4", "swp22")
    assert invoke("--intent", "hall-mini.yaml").exit_code == 1
    fake.move_cable(intent, GOLDEN, "leaf4", "swp21")  # the technician moves it back
    res = invoke("--intent", "hall-mini.yaml")
    assert res.exit_code == 0 and "8 / 8 links match intent" in res.output


def test_unreadable_intent_exits_2(workdir):
    res = invoke("--site", "hall-b", "--intent", "missing.yaml")
    assert res.exit_code == 2
    assert res.output.startswith("reading intent      failed: ")


def test_netbox_without_env_exits_2(workdir, monkeypatch):
    monkeypatch.delenv("NETBOX_URL", raising=False)
    res = invoke("--site", "hall-b", "--intent", "netbox")
    assert res.exit_code == 2 and "NETBOX_URL" in res.output


def test_no_source_reachable_exits_2(fake_hall):
    intent, fake = fake_hall
    for sw in intent.switches:
        fake.switch_error(sw.name, "nvue: ConnectError")
    for node in intent.nodes:
        fake.node_unreachable(node.name, "ConnectError")
        fake.node_dcgm_unreachable(node.name, "ConnectError")
    res = invoke("--intent", "hall-mini.yaml")
    assert res.exit_code == 2, res.output
    assert G("[x] no telemetry source reachable") in res.output


def test_json_flag_prints_report_to_stdout_and_layout_to_stderr(fake_hall):
    intent, fake = fake_hall
    fake.move_cable(intent, GOLDEN, "leaf4", "swp22")
    res = CliRunner().invoke(cli.main, ["--intent", "hall-mini.yaml", "--json"], catch_exceptions=False)
    assert res.exit_code == 1
    doc = json.loads(res.stdout)
    assert doc["schema"] == "turnup.report/v1" and doc["summary"]["findings"] == 1
    assert "move cable one port up" in res.stderr
    assert "reading intent" in res.stderr


def test_out_flag_and_report_line(fake_hall, workdir):
    res = invoke("--intent", "hall-mini.yaml", "--out", str(workdir / "reports" / "run1.json"))
    assert res.exit_code == 0
    assert (workdir / "reports" / "run1.json").exists()
    assert f"report       {workdir / 'reports' / 'run1.json'}" in res.output


def test_only_lldp_polls_only_switches(fake_hall):
    _intent, fake = fake_hall
    fake.node_unreachable("node7")
    res = invoke("--intent", "hall-mini.yaml", "--only", "lldp")
    assert res.exit_code == 0, res.output
    assert "polling             lldp\n" in res.output
    assert "nodes reachable" not in res.output
    assert {kind for kind, _ in fake.calls} == {"lldp"}


def test_only_with_unknown_source_exits_2(fake_hall):
    res = invoke("--intent", "hall-mini.yaml", "--only", "lldp,magic")
    assert res.exit_code == 2 and "unknown source" in res.output


def test_telemetry_flag_sends_one_ping_after_the_report(fake_hall, monkeypatch, workdir):
    intent, fake = fake_hall
    fake.move_cable(intent, GOLDEN, "leaf4", "swp22")
    sent: list[dict] = []

    def fake_send(payload, **kw):
        sent.append(payload)
        return True, "sent (1 rack, 7 links)"

    monkeypatch.setattr(cli, "send_ping", fake_send)
    res = invoke("--intent", "hall-mini.yaml", "--telemetry")
    assert res.exit_code == 1
    assert "share stats  sent (1 rack, 7 links)" in res.output
    (payload,) = sent
    assert payload["schema"] == "turnup.ping/v1" and payload["simulated"] is True
    assert payload["racks_checked"] == 1 and payload["links_verified"] == 7 and payload["nodes_checked"] == 2
    assert set(payload) == {
        "schema",
        "install_id",
        "tool_version",
        "simulated",
        "racks_checked",
        "links_verified",
        "nodes_checked",
        "ts",
    }
    assert (workdir / "cfg" / "install_id").exists()


def test_telemetry_failure_never_changes_exit_code(fake_hall, monkeypatch):
    monkeypatch.setattr(
        cli, "send_ping", lambda payload, **kw: (False, "could not reach counter (ConnectError; ignored)")
    )
    res = invoke("--intent", "hall-mini.yaml", "--telemetry")
    assert res.exit_code == 0
    assert "share stats  could not reach counter (ConnectError; ignored)" in res.output


def test_no_telemetry_means_no_install_id(fake_hall, workdir):
    invoke("--intent", "hall-mini.yaml")
    assert not (workdir / "cfg").exists()


def test_site_mismatch_is_a_verbose_note(fake_hall):
    res = invoke("--site", "hall-z", "--intent", "hall-mini.yaml", "-v")
    assert res.exit_code == 0
    assert "note         --site hall-z differs from intent site hall-mini" in res.output


def test_creds_file_and_env(workdir):
    (workdir / "creds.yaml").write_text(
        "bmc: {user: admin, password: pw}\n"
        "switch: {user: ops, password: ps}\n"
        "snmp: {community: c0, version: 2c}\n"
        "devices:\n  node7: {user: root, password: rp}\n  leaf9: {community: zz}\n",
        encoding="utf-8",
    )
    creds = cli.load_creds(workdir / "creds.yaml", env={})
    assert creds.bmc("node8") == ("admin", "pw") and creds.bmc("node7") == ("root", "rp")
    assert creds.switch("leaf4") == ("ops", "ps")
    assert creds.community("leaf9") == "zz" and creds.community("l") == "c0"
    env_only = cli.load_creds(None, env={"TURNUP_BMC_USER": "u", "TURNUP_BMC_PASS": "p"})
    assert env_only.bmc("x") == ("u", "p") and env_only.switch("x") == ("u", "p")
    both = cli.load_creds(
        None, env={"TURNUP_BMC_USER": "u", "TURNUP_SWITCH_USER": "s", "TURNUP_SNMP_COMMUNITY": "k"}
    )
    assert both.switch("x") == ("s", "") and both.community("x") == "k"


def test_bad_creds_file_exits_2(fake_hall, workdir):
    (workdir / "creds.yaml").write_text("- not a mapping\n", encoding="utf-8")
    res = invoke("--intent", "hall-mini.yaml", "--creds", "creds.yaml")
    assert res.exit_code == 2 and "creds" in res.output


def test_version_and_help():
    assert "0.1.0" in invoke("--version").output
    out = invoke("--help").output
    for flag in (
        "--site",
        "--intent",
        "--baseline",
        "--out",
        "--timeout",
        "--json",
        "--telemetry",
        "--only",
        "--creds",
    ):
        assert flag in out


def test_baseline_flag_applies(fake_hall, workdir):
    _intent, fake = fake_hall
    fake.node_firmware("node7", "gpu_vbios", "96.00")
    (workdir / "baseline.yaml").write_text(
        "GB300 NVL72 compute tray: {gpu_vbios: '97.00'}\n", encoding="utf-8"
    )
    res = invoke("--intent", "hall-mini.yaml", "--baseline", "baseline.yaml")
    assert res.exit_code == 1
    assert G("[x] firmware   node7 gpu_vbios 96.00") in res.output
    assert "fix          update gpu_vbios on node7 to 97.00" in res.output


def test_nic_port_details_are_fetched_only_for_nodes_with_link_findings(fake_hall):
    intent, fake = fake_hall
    fake.strict_ports = True
    fake.unseat(intent, GOLDEN, nic_down=True)
    res = invoke("--intent", "hall-mini.yaml")
    assert res.exit_code == 1, res.output
    # The "check NIC" fix needs the node's Redfish port state, which only the second pass fetches.
    assert "fix          check NIC mlx5_3 on node7" in res.output
    assert fake.calls.count(("redfish", "node7")) == 1 and fake.calls.count(("redfish", "node8")) == 1
    assert fake.calls.count(("redfish+ports", "node7")) == 1
    assert ("redfish+ports", "node8") not in fake.calls


def test_no_second_pass_when_links_are_clean(fake_hall):
    _intent, fake = fake_hall
    fake.strict_ports = True
    fake.node_firmware("node7", "bmc", "0.1")
    res = invoke("--intent", "hall-mini.yaml")
    assert res.exit_code == 1
    assert not [c for c in fake.calls if c[0] == "redfish+ports"]


@pytest.mark.parametrize("value", ["nan", "0", "-1", "-0.5", "inf"])
def test_bad_timeout_is_a_usage_error_not_a_traceback(fake_hall, value):
    res = CliRunner().invoke(cli.main, ["--intent", "hall-mini.yaml", "--timeout", value])
    assert res.exit_code == 2, res.output
    assert "Invalid value for '--timeout'" in res.output
    assert "Traceback" not in res.output and isinstance(res.exception, SystemExit)


@pytest.mark.parametrize("value", [float("nan"), 0.0, -3.0])
async def test_bad_timeout_through_run_options_exits_2(fake_hall, workdir, value):
    import io

    out = io.StringIO()
    code = await cli.run(
        cli.RunOptions(site=None, intent="hall-mini.yaml", timeout=value, stdout=out, cwd=workdir)
    )
    assert code == 2
    assert "--timeout must be a positive number of seconds" in out.getvalue()
    assert not list(workdir.glob("hall-mini-*.json"))  # nothing polled, no report
