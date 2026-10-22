# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Diff engine: every finding class, every fix sentence, the wrong_port correlation, and unknowns."""

from __future__ import annotations

import pytest
from helpers import FakeCollector, load_text

from turnup_check.collectors import poll_all
from turnup_check.diff import diff, missing_link_fix, nodes_needing_ports, wrong_port_fix
from turnup_check.models import Cable, CableEnd, Intent

GOLDEN = "CBL-12-07-03"  # node7 mlx5_3 -> leaf4/swp21


async def run(intent: Intent, fake: FakeCollector, **kw):
    obs = await poll_all(intent, fake, timeout=1.0, **kw)
    return diff(intent, obs)


def classes(result) -> list[str]:
    return [f.class_ for f in result.findings]


# -- baseline ------------------------------------------------------------------------------------


async def test_all_correct_hall_has_no_findings(mini_intent, fake):
    res = await run(mini_intent, fake)
    assert res.findings == []
    assert res.summary.links_ok == res.summary.links_total == 8
    assert res.summary.nodes_reachable == res.summary.nodes_total == 2
    assert res.summary.firmware_ok is True
    assert res.summary.gpus_expected == res.summary.gpus_seen == 8


# -- wrong_port and its fix sentences ----------------------------------------------------------------


async def test_wrong_port_one_up_is_the_golden_finding(mini_intent, fake):
    fake.move_cable(mini_intent, GOLDEN, "leaf4", "swp22")
    res = await run(mini_intent, fake)
    assert classes(res) == ["wrong_port"]
    f = res.findings[0]
    assert f.id == "f-0001"
    assert f.severity == "high"
    assert (f.cable, f.device, f.interface) == (GOLDEN, "node7", "mlx5_3")
    assert (f.expected, f.observed) == ("leaf4/21", "leaf4/22")
    assert f.diagnosis == "neighbor mismatch"
    assert f.fix == "move cable one port up"
    assert f.evidence["leaf4/21"] == {"link_up": False, "neighbor": None}
    assert f.evidence["leaf4/22"] == {"link_up": True, "speed_gbps": 400, "neighbor": "node7 mlx5_3"}
    assert res.summary.links_ok == 7 and res.summary.links_total == 8


async def test_wrong_port_one_down(mini_intent, fake):
    fake.move_cable(mini_intent, GOLDEN, "leaf4", "swp20")
    res = await run(mini_intent, fake)
    assert classes(res) == ["wrong_port"]
    assert res.findings[0].fix == "move cable one port down"


async def test_wrong_port_k_ports_up_and_down(mini_intent, fake):
    fake.move_cable(mini_intent, GOLDEN, "leaf4", "swp24")
    res = await run(mini_intent, fake)
    assert classes(res) == ["wrong_port"]
    assert res.findings[0].fix == "move cable 3 ports up"

    fake2 = FakeCollector.from_intent(mini_intent)
    fake2.move_cable(mini_intent, GOLDEN, "leaf4", "swp17")
    res2 = await run(mini_intent, fake2)
    assert res2.findings[0].fix == "move cable 4 ports down"


async def test_wrong_port_far_away_uses_from_to_sentence(mini_intent, fake):
    fake.move_cable(mini_intent, GOLDEN, "leaf4", "swp30")
    res = await run(mini_intent, fake)
    assert classes(res) == ["wrong_port"]
    assert res.findings[0].fix == "move cable from leaf4/30 to leaf4/21"
    assert res.findings[0].observed == "leaf4/30"


async def test_wrong_port_other_switch_even_when_adjacent_number(mini_intent, fake):
    fake.move_cable(mini_intent, GOLDEN, "leaf3", "swp5")
    res = await run(mini_intent, fake)
    assert classes(res) == ["wrong_port"]
    assert res.findings[0].fix == "move cable from leaf3/5 to leaf4/21"


def test_fix_sentence_table_directly():
    assert wrong_port_fix("leaf4", "swp21", "leaf4", "swp22") == "move cable one port up"
    assert wrong_port_fix("leaf4", "swp21", "leaf4", "swp20") == "move cable one port down"
    assert wrong_port_fix("leaf4", "swp21", "leaf4", "swp23") == "move cable 2 ports up"
    assert wrong_port_fix("leaf4", "swp21", "leaf4", "swp25") == "move cable 4 ports up"
    assert wrong_port_fix("leaf4", "swp21", "leaf4", "swp19") == "move cable 2 ports down"
    assert wrong_port_fix("leaf4", "swp21", "leaf4", "swp26") == "move cable from leaf4/26 to leaf4/21"
    assert wrong_port_fix("leaf4", "swp21", "leaf3", "swp22") == "move cable from leaf3/22 to leaf4/21"
    # Arista names and breakouts: arithmetic only on simple names.
    assert wrong_port_fix("leaf4", "Ethernet21", "leaf4", "Ethernet22") == "move cable one port up"
    assert (
        wrong_port_fix("leaf4", "Ethernet21/1", "leaf4", "Ethernet22/1")
        == "move cable from leaf4/22/1 to leaf4/21/1"
    )


# -- swap_pair: two wrong_port, no missing, no unexpected ------------------------------------------


async def test_swapped_pair_gives_two_wrong_ports_and_nothing_else(mini_intent, fake):
    fake.swap_pair(mini_intent, "CBL-12-07-01", "CBL-12-08-01")  # leaf4/swp1 <-> leaf4/swp3
    res = await run(mini_intent, fake)
    assert sorted(classes(res)) == ["wrong_port", "wrong_port"]
    fixes = {f.cable: f.fix for f in res.findings}
    assert fixes == {"CBL-12-07-01": "move cable 2 ports up", "CBL-12-08-01": "move cable 2 ports down"}
    assert res.summary.links_ok == 6


# -- missing_link ----------------------------------------------------------------------------------


async def test_missing_link_with_nic_down_says_check_nic(mini_intent, fake):
    fake.unseat(mini_intent, GOLDEN, nic_down=True)
    res = await run(mini_intent, fake)
    assert classes(res) == ["missing_link"]
    f = res.findings[0]
    assert f.fix == "check NIC mlx5_3 on node7"
    assert f.expected == "leaf4/21"
    assert f.evidence["leaf4/21"] == {"link_up": False, "neighbor": None}
    assert f.evidence["node7 mlx5_3"]["link_up"] is False


async def test_missing_link_with_nothing_seated_at_the_nic_says_seat_cable(mini_intent, fake):
    """Redfish ``NoLink`` means no cable at the NIC (what simhall renders for an unseated cable):
    the fix is to seat the cable; only ``LinkDown`` points at the NIC."""
    fake.unseat(mini_intent, GOLDEN, nic_down=False)
    fake._set_node_port(
        "node7", "mlx5_3", link_status="NoLink", speed_gbps=0, lldp_system=None, lldp_port=None
    )
    res = await run(mini_intent, fake)
    assert classes(res) == ["missing_link"]
    f = res.findings[0]
    assert f.fix == "seat cable CBL-12-07-03 at leaf4/21"
    assert f.evidence["node7 mlx5_3"] == {"link_up": False, "link_status": "NoLink", "speed_gbps": 0}
    assert f.diagnosis.endswith("node7 mlx5_3 reports NoLink")


async def test_missing_link_with_nic_up_says_seat_cable(mini_intent, fake):
    fake.unseat(mini_intent, GOLDEN, nic_down=False)
    res = await run(mini_intent, fake)
    assert classes(res) == ["missing_link"]
    assert res.findings[0].fix == "seat cable CBL-12-07-03 at leaf4/21"


async def test_missing_link_without_redfish_says_seat_cable(mini_intent, fake):
    fake.unseat(mini_intent, GOLDEN, nic_down=True)
    res = await run(mini_intent, fake, only="lldp")
    assert classes(res) == ["missing_link"]
    assert res.findings[0].fix == "seat cable CBL-12-07-03 at leaf4/21"


def test_missing_link_fix_without_label_falls_back_to_display_names():
    cable = Cable(
        label="",
        a=CableEnd(device="node7", interface="mlx5_3"),
        b=CableEnd(device="leaf4", interface="swp21"),
    )
    assert (
        missing_link_fix(cable, "leaf4", "swp21", "node7", "mlx5_3", False)
        == "seat cable node7 mlx5_3 -> leaf4/21 at leaf4/21"
    )
    assert missing_link_fix(cable, "leaf4", "swp21", "node7", "mlx5_3", True) == "check NIC mlx5_3 on node7"


# -- unexpected_link -------------------------------------------------------------------------------


async def test_unexpected_link_on_spare_port_is_informational(mini_intent, fake):
    fake.add_neighbor("leaf4", "swp30", "storage1", "eth0")
    res = await run(mini_intent, fake)
    assert classes(res) == ["unexpected_link"]
    f = res.findings[0]
    assert f.severity == "info"
    assert (f.device, f.interface) == ("leaf4", "swp30")
    assert f.observed == "storage1 eth0"
    assert res.counted == []
    assert res.summary.links_ok == 8


async def test_wrong_port_neighbor_is_not_also_unexpected(mini_intent, fake):
    fake.move_cable(mini_intent, GOLDEN, "leaf4", "swp22")
    res = await run(mini_intent, fake)
    assert "unexpected_link" not in classes(res)


async def test_unknown_neighbor_on_intended_port_gives_missing_and_unexpected(mini_intent, fake):
    fake.unseat(mini_intent, GOLDEN, nic_down=False)
    fake.add_neighbor("leaf4", "swp21", "mystery", "p1")
    res = await run(mini_intent, fake)
    assert sorted(classes(res)) == ["missing_link", "unexpected_link"]
    missing = next(f for f in res.findings if f.class_ == "missing_link")
    assert "mystery p1" in missing.diagnosis


# -- link_degraded ---------------------------------------------------------------------------------


async def test_link_degraded_speed_below_intent(mini_intent, fake):
    fake.degrade(mini_intent, GOLDEN, speed_gbps=200)
    res = await run(mini_intent, fake)
    assert classes(res) == ["link_degraded"]
    f = res.findings[0]
    assert f.severity == "medium"
    assert f.observed == "leaf4/21 200G"
    assert f.expected == "leaf4/21 up 400G"
    assert "200G below intent 400G" in f.diagnosis
    assert f.fix.startswith("check transceivers and cable CBL-12-07-03 at leaf4/21")
    assert res.summary.links_ok == 7


async def test_link_degraded_port_down(mini_intent, fake):
    fake.degrade(mini_intent, GOLDEN, down=True)
    res = await run(mini_intent, fake)
    assert classes(res) == ["link_degraded"]
    assert res.findings[0].observed == "leaf4/21 down"
    assert res.findings[0].fix == "check cable CBL-12-07-03 at leaf4/21 and NIC mlx5_3 on node7"


async def test_faster_than_intent_is_fine(mini_intent, fake):
    fake.degrade(mini_intent, GOLDEN, speed_gbps=800)
    res = await run(mini_intent, fake)
    assert res.findings == []


# -- MAC-mode LLDP -----------------------------------------------------------------------------------


async def test_mac_port_ids_resolve_through_intent_macs(mini_intent, fake):
    fake.mac_mode(mini_intent, "leaf4")
    res = await run(mini_intent, fake)
    assert res.findings == []
    assert res.summary.links_ok == 8


async def test_mac_port_ids_resolve_through_redfish_when_intent_has_no_macs(mini_intent, fake):
    fake.mac_mode(mini_intent, "leaf4")
    for dev in mini_intent.devices:
        for itf in dev.interfaces:
            itf.mac = None
    res = await run(mini_intent, fake)
    assert res.findings == []


async def test_mac_mode_wrong_port_still_correlates(mini_intent, fake):
    fake.move_cable(mini_intent, GOLDEN, "leaf4", "swp22")
    fake.mac_mode(mini_intent, "leaf4")
    res = await run(mini_intent, fake)
    assert classes(res) == ["wrong_port"]
    assert res.findings[0].fix == "move cable one port up"


# -- nodes -----------------------------------------------------------------------------------------


async def test_unreachable_node(mini_intent, fake):
    fake.node_unreachable("node8", "timeout after 3 s")
    res = await run(mini_intent, fake)
    assert classes(res) == ["unreachable_node"]
    f = res.findings[0]
    assert f.device == "node8" and f.severity == "high"
    assert "timeout after 3 s" in f.diagnosis
    assert f.fix == "check BMC reachability and credentials for node8"
    assert res.summary.nodes_reachable == 1


async def test_firmware_off_baseline_one_finding_per_component(mini_intent, fake):
    fake.node_firmware("node7", "nic", "28.42.1000")
    fake.node_firmware("node7", "bmc", "1.11.0")
    res = await run(mini_intent, fake)
    assert classes(res) == ["firmware_off_baseline", "firmware_off_baseline"]
    by_comp = {f.interface: f for f in res.findings}
    assert by_comp["nic"].expected == "28.43.1014" and by_comp["nic"].observed == "28.42.1000"
    assert by_comp["bmc"].fix == "update bmc on node7 to 1.12.0"
    assert res.summary.firmware_ok is False


async def test_partial_redfish_reports_unknown_not_mismatch(mini_intent, fake):
    fake.node_partial("node7")
    res = await run(mini_intent, fake)
    assert res.findings == []
    assert res.summary.firmware_ok is True
    assert any("node7: firmware unknown" in n for n in res.notes)


async def test_unknown_component_version_never_fails(mini_intent, fake):
    del fake.redfish_obs["node7"].firmware["nic"]
    res = await run(mini_intent, fake)
    assert res.findings == []
    assert any("firmware nic unknown" in n for n in res.notes)


async def test_node_without_baseline_is_not_compared(mini_intent, fake):
    mini_intent.firmware_baseline = {}
    fake.node_firmware("node7", "nic", "0.0.1")
    res = await run(mini_intent, fake)
    assert res.findings == []


async def test_node_health_warning(mini_intent, fake):
    fake.node_health("node7", "Warning")
    res = await run(mini_intent, fake)
    assert classes(res) == ["node_health_warning"]
    f = res.findings[0]
    assert (f.expected, f.observed, f.severity) == ("OK", "Warning", "medium")


async def test_gpu_count_mismatch(mini_intent, fake):
    fake.node_dcgm("node7", load_text("dcgm/missing_gpu.txt"))
    res = await run(mini_intent, fake)
    assert classes(res) == ["gpu_count_mismatch"]
    f = res.findings[0]
    assert (f.expected, f.observed) == ("4", "3")
    assert f.fix == "reseat or replace the missing GPU on node7"
    assert res.summary.gpus_seen == 7


async def test_gpu_unhealthy_is_per_gpu(mini_intent, fake):
    fake.node_dcgm("node7", load_text("dcgm/xid.txt"))
    res = await run(mini_intent, fake)
    assert classes(res) == ["gpu_unhealthy"]
    f = res.findings[0]
    assert f.interface == "gpu2"
    assert f.observed == "XID 79"
    assert f.fix == "drain node7 and open an RMA packet for GPU 2"


async def test_gpu_unhealthy_ecc_and_remap_two_gpus(mini_intent, fake):
    fake.node_dcgm("node7", load_text("dcgm/ecc_and_remap.txt"))
    res = await run(mini_intent, fake)
    assert classes(res) == ["gpu_unhealthy", "gpu_unhealthy"]
    assert [f.interface for f in res.findings] == ["gpu0", "gpu3"]
    assert res.findings[0].observed == "ECC double-bit errors 3"
    assert res.findings[1].observed == "row remap failure"


async def test_nvlink_degraded(mini_intent, fake):
    fake.node_dcgm("node7", load_text("dcgm/nvlink_crc.txt"))
    res = await run(mini_intent, fake)
    assert classes(res) == ["nvlink_degraded"]
    assert res.findings[0].severity == "medium"


async def test_dcgm_unreachable_is_a_note_not_a_finding(mini_intent, fake):
    fake.node_dcgm_unreachable("node7")
    res = await run(mini_intent, fake)
    assert res.findings == []
    assert any("node7: dcgm unreachable" in n and "gpu: unknown" in n for n in res.notes)
    assert res.summary.gpus_seen == 4


# -- switches ----------------------------------------------------------------------------------------


async def test_unreachable_switch_is_reported_and_its_links_are_not_ok(mini_intent, fake):
    fake.switch_error("leaf3", "nvue: timeout after 3 s")
    res = await run(mini_intent, fake)
    assert classes(res) == ["unreachable_node"]
    f = res.findings[0]
    assert f.device == "leaf3"
    assert "4 links not verified" in f.diagnosis
    assert res.summary.links_ok == 4
    assert res.summary.nodes_reachable == 2  # switches never count as nodes


# -- combinations and ordering ----------------------------------------------------------------------


async def test_many_faults_give_exactly_the_expected_multiset(mini_intent, fake):
    fake.move_cable(mini_intent, GOLDEN, "leaf4", "swp22")
    fake.unseat(mini_intent, "CBL-12-08-02", nic_down=True)
    fake.degrade(mini_intent, "CBL-12-07-00", speed_gbps=100)
    fake.node_unreachable("node8")
    fake.node_firmware("node7", "bios", "2.2.0")
    fake.node_dcgm("node7", load_text("dcgm/xid.txt"))
    fake.add_neighbor("leaf3", "swp6", "camera", "eth0")
    res = await run(mini_intent, fake)
    assert sorted(classes(res)) == sorted(
        [
            "wrong_port",
            "missing_link",
            "link_degraded",
            "unreachable_node",
            "firmware_off_baseline",
            "gpu_unhealthy",
            "unexpected_link",
        ]
    )
    # Link findings first (intent cable order), then node findings, then informational.
    assert classes(res)[:3] == ["link_degraded", "wrong_port", "missing_link"]
    assert classes(res)[-1] == "unexpected_link"
    assert [f.id for f in res.findings] == [f"f-{i:04d}" for i in range(1, 8)]
    assert res.summary.findings == 7
    assert len(res.counted) == 6


async def test_only_lldp_skips_node_checks(mini_intent, fake):
    fake.node_unreachable("node8")
    res = await run(mini_intent, fake, only="lldp")
    assert res.findings == []
    assert res.summary.nodes_reachable == 0


async def test_only_redfish_skips_links(mini_intent, fake):
    fake.move_cable(mini_intent, GOLDEN, "leaf4", "swp22")
    res = await run(mini_intent, fake, only="redfish,dcgm")
    assert res.findings == []
    assert res.summary.links_ok == 0


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda i, f: f.move_cable(i, GOLDEN, "leaf4", "swp22"), "wrong_port"),
        (lambda i, f: f.unseat(i, GOLDEN), "missing_link"),
        (lambda i, f: f.add_neighbor("leaf3", "swp6", "x", "y"), "unexpected_link"),
        (lambda i, f: f.degrade(i, GOLDEN, down=True), "link_degraded"),
        (lambda i, f: f.node_unreachable("node7"), "unreachable_node"),
        (lambda i, f: f.node_firmware("node7", "bmc", "0"), "firmware_off_baseline"),
        (lambda i, f: f.node_dcgm("node7", load_text("dcgm/missing_gpu.txt")), "gpu_count_mismatch"),
        (lambda i, f: f.node_dcgm("node7", load_text("dcgm/xid.txt")), "gpu_unhealthy"),
        (lambda i, f: f.node_dcgm("node7", load_text("dcgm/nvlink_zero_bw.txt")), "nvlink_degraded"),
        (lambda i, f: f.node_health("node7", "Critical"), "node_health_warning"),
    ],
)
async def test_every_finding_class_is_producible(mini_intent, fake, mutate, expected):
    mutate(mini_intent, fake)
    res = await run(mini_intent, fake)
    assert classes(res) == [expected]


async def test_nodes_needing_ports_lists_only_nodes_with_link_findings(mini_intent, fake):
    fake.move_cable(mini_intent, GOLDEN, "leaf4", "swp22")
    fake.node_firmware("node8", "bmc", "0")
    fake.add_neighbor("leaf3", "swp6", "camera", "eth0")
    res = await run(mini_intent, fake)
    assert nodes_needing_ports(mini_intent, res) == {"node7"}
    clean = await run(mini_intent, FakeCollector.from_intent(mini_intent))
    assert nodes_needing_ports(mini_intent, clean) == set()
