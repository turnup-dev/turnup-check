# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Vocabulary helpers (CONTRACT section 1) and model derivations (section 2.1)."""

from __future__ import annotations

import pytest

from turnup_check.models import (
    Creds,
    Device,
    DeviceCreds,
    Finding,
    Intent,
    Mgmt,
    gpu_count_for_model,
    node_iface_display,
    normalize_mac,
    port_number,
    port_suffix,
    protocol_for_manufacturer,
    speed_from_interface_type,
    switch_port_display,
)


@pytest.mark.parametrize(
    "switch,ifname,display",
    [
        ("leaf4", "swp21", "leaf4/21"),
        ("leaf4", "Ethernet21", "leaf4/21"),
        ("leaf4", "Ethernet21/1", "leaf4/21/1"),
        ("spine1", "swp1s0", "spine1/1s0"),
    ],
)
def test_switch_port_display(switch, ifname, display):
    assert switch_port_display(switch, ifname) == display


def test_node_iface_display():
    assert node_iface_display("node7", "mlx5_3") == "node7 mlx5_3"


def test_port_number_and_suffix():
    assert port_number("swp21") == 21
    assert port_number("Ethernet21/1") == 21
    assert port_number("mgmt") is None
    assert port_suffix("Ethernet21/1") == "21/1"
    assert port_suffix("mgmt") == "mgmt"


@pytest.mark.parametrize(
    "model,count",
    [
        ("GB300 NVL72 compute tray", 4),
        ("GB200 NVL72", 4),
        ("HGX H100 8-GPU", 8),
        ("MI300X platform", 8),
        ("MI355X", 8),
        (None, 8),
        ("something else", 8),
    ],
)
def test_gpu_count_by_model(model, count):
    assert gpu_count_for_model(model) == count
    assert Device(name="n", role="gpu-node", model=model).expected_gpus == count


def test_explicit_gpu_count_wins():
    assert Device(name="n", role="gpu-node", model="HGX", gpu_count=2).expected_gpus == 2


@pytest.mark.parametrize(
    "manufacturer,proto",
    [
        ("Arista", "eapi"),
        ("Arista Networks", "eapi"),
        ("NVIDIA", "nvue"),
        ("Mellanox", "nvue"),
        ("Cumulus", "nvue"),
        ("Cisco", "snmp"),
        (None, "snmp"),
    ],
)
def test_protocol_by_manufacturer(manufacturer, proto):
    assert protocol_for_manufacturer(manufacturer) == proto
    assert Device(name="s", role="leaf-switch", manufacturer=manufacturer).lldp_protocol == proto


def test_explicit_protocol_wins():
    d = Device(name="s", role="leaf-switch", manufacturer="Arista", mgmt=Mgmt(protocol="snmp"))
    assert d.lldp_protocol == "snmp"


def test_normalize_mac_forms():
    assert normalize_mac("B8:3F:D2:00:07:03") == "b8:3f:d2:00:07:03"
    assert normalize_mac("b83f.d200.0703") == "b8:3f:d2:00:07:03"
    assert normalize_mac("b8-3f-d2-00-07-03") == "b8:3f:d2:00:07:03"
    assert normalize_mac("mlx5_3") is None
    assert normalize_mac("") is None


def test_speed_from_interface_type():
    assert speed_from_interface_type("400gbase-x-osfp") == 400
    assert speed_from_interface_type("200gbase-x-qsfp56") == 200
    assert speed_from_interface_type("infiniband-ndr") is None
    assert speed_from_interface_type(None) is None


def test_intent_schema_is_enforced():
    with pytest.raises(ValueError):
        Intent.model_validate({"schema": "turnup.intent/v2", "site": "x"})
    intent = Intent.model_validate({"schema": "turnup.intent/v1", "site": "x"})
    assert intent.model_dump(by_alias=True)["schema"] == "turnup.intent/v1"
    assert "source" not in intent.model_dump(by_alias=True)


def test_intent_rack_names_merge_rack_list_and_device_racks():
    intent = Intent.model_validate(
        {
            "schema": "turnup.intent/v1",
            "site": "x",
            "racks": [{"name": 12}],
            "devices": [{"name": "n1", "role": "gpu-node", "rack": 13}],
        }
    )
    assert intent.rack_names == ["12", "13"]
    assert intent.summary()["racks"] == 2


def test_finding_class_alias_and_validation():
    f = Finding.model_validate({"class": "wrong_port", "device": "node7"})
    assert f.class_ == "wrong_port"
    assert f.as_json()["class"] == "wrong_port"
    with pytest.raises(ValueError):
        Finding(class_="bogus")


def test_creds_precedence():
    creds = Creds(
        bmc_user="admin",
        bmc_pass="pw",
        switch_user="ops",
        devices={"node7": DeviceCreds(user="root", password="x")},
    )
    assert creds.bmc("node8") == ("admin", "pw")
    assert creds.bmc("node7") == ("root", "x")
    assert creds.switch("leaf4") == ("ops", "")
    assert Creds().bmc("node7") is None
    assert Creds().community("leaf4") == "public"
    assert Creds(devices={"leaf4": DeviceCreds(community="s3cr3t")}).community("leaf4") == "s3cr3t"
