# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Regenerates the JSON fixtures in this directory (JSON cannot carry a license header).

Run: ``python tests/fixtures/make_fixtures.py``. Shapes follow the public vendor documentation
quoted in CONTRACT.md section 3 and the NetBox/Nautobot REST API docs. All names, MACs and
serials are synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent


def dump(rel: str, doc: object) -> None:
    path = HERE / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")


def mac(node: int, port: int) -> str:
    return f"b8:3f:d2:00:{node:02x}:{port:02x}"


# ----------------------------------------------------------------------------------------------
# Arista eAPI: leaf4 with node7 mlx5_3 on Ethernet21, Ethernet22 spare, Ethernet23 MAC-mode neighbor
# ----------------------------------------------------------------------------------------------


def eapi_leaf4() -> dict:
    lldp = {
        "lldpNeighbors": {
            "Ethernet21": {
                "lldpNeighborInfo": [
                    {
                        "chassisId": mac(7, 0),
                        "chassisIdType": "macAddress",
                        "systemName": "node7",
                        "neighborInterfaceInfo": {
                            "interfaceIdType": "interfaceName",
                            "interfaceId": "mlx5_3",
                            "interfaceDescription": "mlx5_3",
                        },
                    }
                ]
            },
            "Ethernet23": {
                "lldpNeighborInfo": [
                    {
                        "chassisId": mac(8, 0),
                        "chassisIdType": "macAddress",
                        "systemName": "node8",
                        "neighborInterfaceInfo": {
                            "interfaceIdType": "macAddress",
                            "interfaceId": mac(8, 1),
                            "interfaceDescription": "",
                        },
                    }
                ]
            },
        }
    }
    status = {
        "interfaceStatuses": {
            "Ethernet21": {"linkStatus": "connected", "bandwidth": 400000000000},
            "Ethernet22": {"linkStatus": "notconnect", "bandwidth": 0},
            "Ethernet23": {"linkStatus": "connected", "bandwidth": 200000000000},
            "Ethernet24": {"linkStatus": "connected", "bandwidth": 400000000000},
            "Ethernet25": {"linkStatus": "disabled", "bandwidth": 0},
        }
    }
    return {"jsonrpc": "2.0", "id": "turnup", "result": [lldp, status]}


# ----------------------------------------------------------------------------------------------
# NVIDIA NVUE: same leaf, same ports as swp21..swp25
# ----------------------------------------------------------------------------------------------


def nvue_leaf4() -> dict:
    return {
        "eth0": {"type": "eth", "link": {"state": {"up": {}}, "speed": "1G"}},
        "lo": {"type": "loopback"},
        "swp21": {
            "type": "swp",
            "link": {"state": {"up": {}}, "speed": "400G"},
            "lldp": {
                "neighbor": {
                    "node7": {
                        "chassis": {"chassis-id": mac(7, 0), "system-name": "node7"},
                        "port": {"id": "mlx5_3", "id-type": "if_name"},
                    }
                }
            },
        },
        "swp22": {"type": "swp", "link": {"state": {"down": {}}, "speed": "auto"}},
        "swp23": {
            "type": "swp",
            "link": {"state": {"up": {}}, "speed": "200G"},
            "lldp": {
                "neighbor": {
                    "node8": {
                        "chassis": {"chassis-id": mac(8, 0), "system-name": "node8"},
                        "port": {"id": mac(8, 1), "id-type": "mac"},
                    }
                }
            },
        },
        "swp24": {"type": "swp", "link": {"state": {"up": {}}, "speed": "400G"}},
        "swp25": {"type": "swp", "link": {"state": {"down": {}}}},
    }


# ----------------------------------------------------------------------------------------------
# Redfish tree for node7 (paths relative to the device prefix)
# ----------------------------------------------------------------------------------------------


def redfish_node7(*, partial: bool = False, health: str = "OK", nic_version: str = "28.43.1014") -> dict:
    root = "/redfish/v1"
    sysid = "System.Embedded.1"
    tree: dict[str, dict] = {
        f"{root}/": {
            "@odata.id": f"{root}/",
            "RedfishVersion": "1.17.0",
            "Id": "RootService",
            "Systems": {"@odata.id": f"{root}/Systems"},
            "Chassis": {"@odata.id": f"{root}/Chassis"},
            "Managers": {"@odata.id": f"{root}/Managers"},
            "UpdateService": {"@odata.id": f"{root}/UpdateService"},
        },
        f"{root}/Systems": {
            "@odata.id": f"{root}/Systems",
            "Members@odata.count": 1,
            "Members": [{"@odata.id": f"{root}/Systems/{sysid}"}],
        },
        f"{root}/Systems/{sysid}": {
            "@odata.id": f"{root}/Systems/{sysid}",
            "Id": sysid,
            "HostName": "node7",
            "Manufacturer": "NVIDIA",
            "Model": "GB300 NVL72 compute tray",
            "SerialNumber": "SN-N7-000123",
            "BiosVersion": "2.3.1",
            "PowerState": "On",
            "Status": {"Health": health, "State": "Enabled"},
            "ProcessorSummary": {"Count": 2},
        },
        f"{root}/Managers": {
            "@odata.id": f"{root}/Managers",
            "Members": [{"@odata.id": f"{root}/Managers/BMC"}],
        },
        f"{root}/Managers/BMC": {
            "@odata.id": f"{root}/Managers/BMC",
            "Id": "BMC",
            "FirmwareVersion": "1.12.0",
            "Status": {"Health": "OK", "State": "Enabled"},
        },
        f"{root}/UpdateService": {
            "@odata.id": f"{root}/UpdateService",
            "FirmwareInventory": {"@odata.id": f"{root}/UpdateService/FirmwareInventory"},
        },
        f"{root}/Chassis": {"@odata.id": f"{root}/Chassis", "Members": [{"@odata.id": f"{root}/Chassis/1"}]},
        f"{root}/Chassis/1": {
            "@odata.id": f"{root}/Chassis/1",
            "Id": "1",
            "NetworkAdapters": {"@odata.id": f"{root}/Chassis/1/NetworkAdapters"},
        },
        f"{root}/Chassis/1/NetworkAdapters": {
            "@odata.id": f"{root}/Chassis/1/NetworkAdapters",
            "Members": [{"@odata.id": f"{root}/Chassis/1/NetworkAdapters/NIC.1"}],
        },
        f"{root}/Chassis/1/NetworkAdapters/NIC.1": {
            "@odata.id": f"{root}/Chassis/1/NetworkAdapters/NIC.1",
            "Id": "NIC.1",
            "Model": "ConnectX-8",
            "Ports": {"@odata.id": f"{root}/Chassis/1/NetworkAdapters/NIC.1/Ports"},
        },
        f"{root}/Chassis/1/NetworkAdapters/NIC.1/Ports": {
            "@odata.id": f"{root}/Chassis/1/NetworkAdapters/NIC.1/Ports",
            "Members": [
                {"@odata.id": f"{root}/Chassis/1/NetworkAdapters/NIC.1/Ports/mlx5_{i}"} for i in range(4)
            ],
        },
    }
    for i in range(4):
        tree[f"{root}/Chassis/1/NetworkAdapters/NIC.1/Ports/mlx5_{i}"] = {
            "@odata.id": f"{root}/Chassis/1/NetworkAdapters/NIC.1/Ports/mlx5_{i}",
            "Id": f"mlx5_{i}",
            "Name": f"mlx5_{i}",
            "LinkStatus": "LinkUp",
            "CurrentSpeedGbps": 400,
            "Ethernet": {
                "AssociatedMACAddresses": [mac(7, i)],
                "LLDPReceive": {
                    "ChassisId": "aa:00:00:00:00:04",
                    "PortId": f"swp{21 + i}",
                    "SystemName": "leaf4",
                },
            },
        }
    if not partial:
        inv = f"{root}/UpdateService/FirmwareInventory"
        members = [
            ("BMC", "BMC Firmware", "1.12.0"),
            ("Previous-BIOS", "System BIOS", "2.2.0"),
            ("BIOS", "System BIOS", "2.3.1"),
            ("NIC.1", "ConnectX-8 Network Adapter", nic_version),
            ("GPU.0", "NVIDIA GPU VBIOS", "97.00.5E.00.01"),
            ("Thermal.1", "Fan Controller", "0.9"),
        ]
        tree[inv] = {"@odata.id": inv, "Members": [{"@odata.id": f"{inv}/{mid}"} for mid, _, _ in members]}
        for mid, name, version in members:
            tree[f"{inv}/{mid}"] = {
                "@odata.id": f"{inv}/{mid}",
                "Id": mid,
                "Name": name,
                "Version": version,
                "Updateable": True,
            }
    return tree


# ----------------------------------------------------------------------------------------------
# NetBox pages (v4 shape, two pages of devices) and NetBox 3 / Nautobot variants
# ----------------------------------------------------------------------------------------------

NB = "http://netbox.example.test"


def nb_device(
    i: int,
    name: str,
    role: str,
    model: str,
    manufacturer: str,
    ip: str,
    *,
    v3: bool = False,
    cf=None,
    cc=None,
):
    d = {
        "id": i,
        "url": f"{NB}/api/dcim/devices/{i}/",
        "name": name,
        "device_type": {
            "id": 1,
            "model": model,
            "manufacturer": {"id": 1, "name": manufacturer, "slug": manufacturer.lower()},
        },
        "serial": f"SN-{name.upper()}",
        "site": {"id": 1, "name": "Hall B", "slug": "hall-b"},
        "rack": {"id": 12, "name": "12", "display": "12"},
        "position": 14.0 if role == "gpu-node" else 40.0,
        "primary_ip4": {"id": i, "address": f"{ip}/24", "family": {"value": 4}},
        "custom_fields": cf or {},
        "config_context": cc or {},
    }
    role_obj = {"id": 2, "name": role.replace("-", " ").title(), "slug": role}
    if v3:
        d["device_role"] = role_obj
    else:
        d["role"] = role_obj
    return d


def netbox_fixtures() -> None:
    cc = {"turnup": {"firmware_baseline": {"bmc": "1.12.0", "bios": "2.3.1", "nic": "28.43.1014"}}}
    cf7 = {
        "turnup_bmc_url": "http://127.0.0.1:8900/dev/node7",
        "turnup_dcgm_url": "http://127.0.0.1:8900/dev/node7",
    }
    node7 = nb_device(1, "node7", "gpu-node", "GB300 NVL72 compute tray", "NVIDIA", "10.0.0.7", cf=cf7, cc=cc)
    node8 = nb_device(2, "node8", "gpu-node", "GB300 NVL72 compute tray", "NVIDIA", "10.0.0.8", cc=cc)
    leaf3 = nb_device(3, "leaf3", "leaf-switch", "SN5600", "NVIDIA", "10.0.1.3")
    leaf4 = nb_device(
        4,
        "leaf4",
        "leaf-switch",
        "SN5600",
        "NVIDIA",
        "10.0.1.4",
        cf={"turnup_switch_url": "http://127.0.0.1:8900/dev/leaf4"},
    )
    pdu = nb_device(5, "pdu1", "pdu", "PX3", "Raritan", "10.0.2.1")
    dump(
        "netbox/devices_page1.json",
        {
            "count": 5,
            "next": f"{NB}/api/dcim/devices/?site=hall-b&limit=1000&offset=3",
            "previous": None,
            "results": [node7, node8, leaf3],
        },
    )
    dump(
        "netbox/devices_page2.json",
        {
            "count": 5,
            "next": None,
            "previous": f"{NB}/api/dcim/devices/?site=hall-b&limit=1000",
            "results": [leaf4, pdu],
        },
    )

    interfaces = []
    iid = 100
    for n, dev in ((7, "node7"), (8, "node8")):
        for p in range(4):
            iid += 1
            itf = {
                "id": iid,
                "device": {"id": n - 6, "name": dev},
                "name": f"mlx5_{p}",
                "type": {"value": "400gbase-x-osfp", "label": "OSFP (400GE)"},
                "mac_address": mac(n, p).upper() if n == 7 else None,
            }
            if n == 8:  # NetBox 4.2+ shape
                itf["primary_mac_address"] = {"id": iid, "mac_address": mac(n, p).upper()}
            interfaces.append(itf)
    for leaf_id, leaf in ((3, "leaf3"), (4, "leaf4")):
        for p in range(1, 33):
            iid += 1
            interfaces.append(
                {
                    "id": iid,
                    "device": {"id": leaf_id, "name": leaf},
                    "name": f"swp{p}",
                    "type": {"value": "400gbase-x-osfp", "label": "OSFP (400GE)"},
                    "mac_address": None,
                }
            )
    dump(
        "netbox/interfaces.json",
        {"count": len(interfaces), "next": None, "previous": None, "results": interfaces},
    )

    def term(dev, ifname, dev_id):
        return [
            {
                "object_type": "dcim.interface",
                "object_id": 0,
                "object": {"id": 0, "name": ifname, "device": {"id": dev_id, "name": dev}},
            }
        ]

    cables = []
    plan = [
        ("CBL-12-07-00", "node7", "mlx5_0", "leaf3", "swp1"),
        ("CBL-12-07-01", "node7", "mlx5_1", "leaf4", "swp1"),
        ("CBL-12-07-02", "node7", "mlx5_2", "leaf3", "swp2"),
        ("CBL-12-07-03", "node7", "mlx5_3", "leaf4", "swp21"),
        ("CBL-12-08-00", "node8", "mlx5_0", "leaf3", "swp3"),
        ("CBL-12-08-01", "node8", "mlx5_1", "leaf4", "swp3"),
        ("CBL-12-08-02", "node8", "mlx5_2", "leaf3", "swp4"),
        ("CBL-12-08-03", "node8", "mlx5_3", "leaf4", "swp4"),
    ]
    for i, (label, nd, ni, lf, li) in enumerate(plan, 1):
        # Put the switch on the A side for odd cables to prove the adapter normalizes sides.
        if i % 2:
            a, b = term(lf, li, 3 if lf == "leaf3" else 4), term(nd, ni, 1 if nd == "node7" else 2)
        else:
            a, b = term(nd, ni, 1 if nd == "node7" else 2), term(lf, li, 3 if lf == "leaf3" else 4)
        cables.append(
            {
                "id": i,
                "label": label,
                "type": "aoc",
                "status": {"value": "connected"},
                "a_terminations": a,
                "b_terminations": b,
            }
        )
    # A power cable (not interface-to-interface) must be skipped.
    cables.append(
        {
            "id": 99,
            "label": "PWR-1",
            "a_terminations": [
                {"object_type": "dcim.powerport", "object": {"name": "PSU1", "device": {"name": "node7"}}}
            ],
            "b_terminations": [
                {"object_type": "dcim.poweroutlet", "object": {"name": "1", "device": {"name": "pdu1"}}}
            ],
        }
    )
    dump("netbox/cables.json", {"count": len(cables), "next": None, "previous": None, "results": cables})
    dump(
        "netbox/racks.json",
        {
            "count": 1,
            "next": None,
            "previous": None,
            "results": [{"id": 12, "name": "12", "site": {"slug": "hall-b"}}],
        },
    )

    dump(
        "netbox/sites.json",
        {
            "count": 1,
            "next": None,
            "previous": None,
            "results": [
                {
                    "id": 1,
                    "name": "Hall B",
                    "slug": "hall-b",
                    "description": "Simulated by simhall",
                    "comments": "",
                    "custom_fields": {},
                }
            ],
        },
    )

    # NetBox 3: device_role + termination_a/termination_b
    n7 = nb_device(1, "node7", "gpu-node", "GB300 NVL72 compute tray", "NVIDIA", "10.0.0.7", v3=True, cc=cc)
    l4 = nb_device(4, "leaf4", "leaf-switch", "DCS-7060DX5-64S", "Arista", "10.0.1.4", v3=True)
    dump("netbox3/devices.json", {"count": 2, "next": None, "previous": None, "results": [n7, l4]})
    dump(
        "netbox3/interfaces.json",
        {
            "count": 2,
            "next": None,
            "previous": None,
            "results": [
                {
                    "id": 1,
                    "device": {"id": 1, "name": "node7"},
                    "name": "mlx5_3",
                    "type": {"value": "400gbase-x-osfp"},
                    "mac_address": mac(7, 3),
                },
                {
                    "id": 2,
                    "device": {"id": 4, "name": "leaf4"},
                    "name": "Ethernet21/1",
                    "type": {"value": "400gbase-x-osfp"},
                    "mac_address": None,
                },
            ],
        },
    )
    dump(
        "netbox3/cables.json",
        {
            "count": 1,
            "next": None,
            "previous": None,
            "results": [
                {
                    "id": 1,
                    "label": "CBL-12-07-03",
                    "termination_a_type": "dcim.interface",
                    "termination_a": {"id": 1, "name": "mlx5_3", "device": {"id": 1, "name": "node7"}},
                    "termination_b_type": "dcim.interface",
                    "termination_b": {"id": 2, "name": "Ethernet21/1", "device": {"id": 4, "name": "leaf4"}},
                }
            ],
        },
    )

    # Nautobot 2.x: role.name, location, uuids
    def nautobot_device(uid, name, role_name, model, manufacturer, ip):
        return {
            "id": uid,
            "name": name,
            "role": {"id": "r-1", "name": role_name},
            "device_type": {
                "id": "dt-1",
                "model": model,
                "manufacturer": {"id": "m-1", "name": manufacturer},
            },
            "location": {"id": "loc-1", "name": "Hall B"},
            "rack": {"id": "rk-12", "name": "12"},
            "position": 14,
            "serial": f"SN-{name.upper()}",
            "primary_ip4": {"id": "ip-1", "address": f"{ip}/24"},
            "custom_fields": {},
            "config_context": {},
        }

    dump(
        "nautobot/devices.json",
        {
            "count": 2,
            "next": None,
            "previous": None,
            "results": [
                nautobot_device("d-1", "node7", "GPU Node", "GB300 NVL72 compute tray", "NVIDIA", "10.0.0.7"),
                nautobot_device("d-4", "leaf4", "Leaf Switch", "SN5600", "NVIDIA", "10.0.1.4"),
            ],
        },
    )
    dump(
        "nautobot/interfaces.json",
        {
            "count": 2,
            "next": None,
            "previous": None,
            "results": [
                {
                    "id": "i-1",
                    "device": {"id": "d-1", "name": "node7"},
                    "name": "mlx5_3",
                    "type": {"value": "400gbase-x-osfp"},
                    "mac_address": mac(7, 3),
                },
                {
                    "id": "i-2",
                    "device": {"id": "d-4", "name": "leaf4"},
                    "name": "swp21",
                    "type": {"value": "400gbase-x-osfp"},
                    "mac_address": None,
                },
            ],
        },
    )
    dump(
        "nautobot/cables.json",
        {
            "count": 1,
            "next": None,
            "previous": None,
            "results": [
                {
                    "id": "c-1",
                    "label": "CBL-12-07-03",
                    "termination_a_type": "dcim.interface",
                    "termination_a": {
                        "id": "i-1",
                        "name": "mlx5_3",
                        "device": {"id": "d-1", "name": "node7"},
                    },
                    "termination_b_type": "dcim.interface",
                    "termination_b": {"id": "i-2", "name": "swp21", "device": {"id": "d-4", "name": "leaf4"}},
                }
            ],
        },
    )


# ----------------------------------------------------------------------------------------------
# DCGM-exporter text (public metric names; label set as the exporter emits it)
# ----------------------------------------------------------------------------------------------

DCGM_LABELS = (
    'UUID="GPU-0000{g}",pci_bus_id="00000000:0{g}:00.0",device="nvidia{g}",'
    'modelName="NVIDIA GB300",Hostname="{host}"'
)


def dcgm_text(
    host: str,
    gpus: int,
    *,
    xid: dict[int, int] | None = None,
    dbe: dict[int, int] | None = None,
    nvlink_crc: dict[int, int] | None = None,
    nvlink_bw_zero: bool = False,
    remap_fail: set[int] | None = None,
) -> str:
    xid = xid or {}
    dbe = dbe or {}
    nvlink_crc = nvlink_crc or {}
    remap_fail = remap_fail or set()
    fams = [
        ("DCGM_FI_DEV_GPU_TEMP", "gauge", "GPU temperature (in C).", lambda g: 41 + g),
        ("DCGM_FI_DEV_POWER_USAGE", "gauge", "Power draw (in W).", lambda g: 120.5),
        ("DCGM_FI_DEV_SM_CLOCK", "gauge", "SM clock frequency (in MHz).", lambda g: 1980),
        ("DCGM_FI_DEV_FB_TOTAL", "gauge", "Framebuffer memory total (in MiB).", lambda g: 196608),
        (
            "DCGM_FI_DEV_XID_ERRORS",
            "gauge",
            "Value of the last XID error encountered.",
            lambda g: xid.get(g, 0),
        ),
        (
            "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL",
            "counter",
            "Total number of volatile double-bit ECC errors.",
            lambda g: dbe.get(g, 0),
        ),
        (
            "DCGM_FI_DEV_ROW_REMAP_FAILURE",
            "gauge",
            "Row remapper failure.",
            lambda g: 1 if g in remap_fail else 0,
        ),
        ("DCGM_FI_DEV_PCIE_REPLAY_COUNTER", "counter", "Total number of PCIe retries.", lambda g: 0),
        (
            "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL",
            "counter",
            "Total number of NVLink flow-control CRC errors.",
            lambda g: nvlink_crc.get(g, 0),
        ),
        (
            "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL",
            "counter",
            "Total number of NVLink bandwidth counters for all lanes.",
            lambda g: 0 if nvlink_bw_zero else 123456789,
        ),
        (
            "DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS",
            "counter",
            "Number of remapped rows for uncorrectable errors.",
            lambda g: 0,
        ),
    ]
    lines = []
    for name, typ, help_, fn in fams:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {typ}")
        for g in range(gpus):
            lines.append(f'{name}{{gpu="{g}",{DCGM_LABELS.format(g=g, host=host)}}} {fn(g)}')
    return "\n".join(lines) + "\n"


def dcgm_fixtures() -> None:
    (HERE / "dcgm").mkdir(exist_ok=True)
    (HERE / "dcgm" / "healthy.txt").write_text(dcgm_text("node7", 4), encoding="utf-8")
    (HERE / "dcgm" / "xid.txt").write_text(dcgm_text("node7", 4, xid={2: 79}), encoding="utf-8")
    (HERE / "dcgm" / "missing_gpu.txt").write_text(dcgm_text("node7", 3), encoding="utf-8")
    (HERE / "dcgm" / "nvlink_crc.txt").write_text(
        dcgm_text("node7", 4, nvlink_crc={1: 250}), encoding="utf-8"
    )
    (HERE / "dcgm" / "nvlink_zero_bw.txt").write_text(
        dcgm_text("node7", 4, nvlink_bw_zero=True), encoding="utf-8"
    )
    (HERE / "dcgm" / "single_gpu_zero_bw.txt").write_text(
        dcgm_text("node9", 1, nvlink_bw_zero=True), encoding="utf-8"
    )
    (HERE / "dcgm" / "ecc_and_remap.txt").write_text(
        dcgm_text("node7", 4, dbe={0: 3}, remap_fail={3}), encoding="utf-8"
    )


def main() -> None:
    dump("eapi/leaf4.json", eapi_leaf4())
    dump("nvue/leaf4.json", nvue_leaf4())
    dump("redfish/node7_tree.json", redfish_node7())
    dump("redfish/node7_partial_tree.json", redfish_node7(partial=True))
    dump("redfish/node7_warning_tree.json", redfish_node7(health="Warning", nic_version="28.42.1000"))
    netbox_fixtures()
    dcgm_fixtures()


if __name__ == "__main__":
    main()
