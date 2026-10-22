# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Regenerates ``<tree>/tree.json`` from a checkout of DMTF Redfish-Publications (DSP2043 mockups).

Run: ``python tests/fixtures/dmtf/extract.py /path/to/Redfish-Publications/mockups``. Keeps only the
resources the Redfish collector reads (recorded from a real walk, plus a few extra paths per tree)
and only the fields it uses. The mockup content is DMTF's, BSD-3-Clause; see LICENSE here.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from turnup_check.collectors.redfish import DictFetcher, walk_redfish

HERE = Path(__file__).parent
BUNDLE = (
    "DMTF Redfish-Publications tag 2026.2 (DSP2043 2026.2), commit 4f81814e399213c9055863ddaff42791c6b3706a"
)

TREES: dict[str, list[str]] = {
    "public-rackmount1": [],
    "public-smartnic": [
        "/redfish/v1/Systems/Card1-SoC",
        "/redfish/v1/Systems/437XR1138R2/NetworkInterfaces",
        "/redfish/v1/Systems/437XR1138R2/NetworkInterfaces/4AWAII",
        "/redfish/v1/Systems/437XR1138R2/NetworkInterfaces/4AWAII/Ports",
        "/redfish/v1/Systems/437XR1138R2/NetworkInterfaces/4AWAIJ",
        "/redfish/v1/Systems/437XR1138R2/NetworkInterfaces/4AWAIJ/Ports",
    ],
    "public-acd": [],
    "public-bladed": ["/redfish/v1/Managers/Blade1BMC"],
    "public-pdu": [],
}

KEEP = {
    "@odata.id", "Id", "Name", "Members", "RedfishVersion", "Systems", "Chassis", "Managers",
    "UpdateService", "FirmwareInventory", "Status", "PowerState", "SerialNumber", "Model",
    "Manufacturer", "BiosVersion", "ProcessorSummary", "HostName", "SystemType", "Links",
    "FirmwareVersion", "ManagerType", "Version", "Updateable", "NetworkAdapters", "Ports",
    "NetworkPorts", "NetworkInterfaces", "EthernetInterfaces", "LinkStatus", "CurrentSpeedGbps",
    "CurrentLinkSpeedMbps", "SpeedMbps", "Ethernet", "AssociatedNetworkAddresses", "MACAddress",
    "PermanentMACAddress",
}  # fmt: skip
KEEP_LINKS = {"ManagedBy", "NetworkAdapter"}


def load_mockup(root: Path) -> dict[str, dict]:
    tree: dict[str, dict] = {}
    for f in root.rglob("index.json"):
        doc = json.loads(f.read_text(encoding="utf-8"))
        rel = f.parent.relative_to(root).as_posix()
        tree["/redfish/v1/" if rel == "." else f"/redfish/v1/{rel}"] = doc
    return tree


def trim(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k in KEEP}
    if isinstance(out.get("Links"), dict):
        out["Links"] = {k: v for k, v in out["Links"].items() if k in KEEP_LINKS}
    if isinstance(out.get("ProcessorSummary"), dict):
        out["ProcessorSummary"] = {"Count": out["ProcessorSummary"].get("Count")}
    if isinstance(out.get("Ethernet"), dict):
        out["Ethernet"] = {
            k: v for k, v in out["Ethernet"].items() if k in ("AssociatedMACAddresses", "LLDPReceive")
        }
    return out


async def extract(mockups: Path, name: str, extra: list[str]) -> None:
    full = load_mockup(mockups / name)
    fetch = DictFetcher(full)
    await walk_redfish(fetch, timeout=5.0, want_ports=True)
    keep = {DictFetcher._norm(p) for p in [*fetch.calls, *extra]}
    sub = {k: trim(v) for k, v in full.items() if DictFetcher._norm(k) in keep}
    out = {"_source": f"{BUNDLE}, mockups/{name}; BSD-3-Clause, see LICENSE", **sub}
    path = HERE / name / "tree.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    for tree_name, extra_paths in TREES.items():
        asyncio.run(extract(Path(sys.argv[1]), tree_name, extra_paths))
