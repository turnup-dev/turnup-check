# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Navigation: one "Turnup check" item under the Plugins menu."""

from __future__ import annotations

try:
    from netbox.plugins import PluginMenuItem  # NetBox >= 3.7
except ImportError:  # pragma: no cover - NetBox 3.5 / 3.6
    from extras.plugins import PluginMenuItem

menu_items = (
    PluginMenuItem(
        link="plugins:netbox_turnup_check:report_list",
        link_text="Turnup check",
        permissions=["dcim.view_device"],
    ),
)
