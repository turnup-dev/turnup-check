# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""NetBox plugin: a "Turnup check" navigation item and a view listing turnup-check report files.

Skeleton only: it has not been run against a NetBox instance yet (none is available in this repo).
Install next to NetBox, add ``"netbox_turnup_check"`` to ``PLUGINS`` and point ``report_dir`` at the
directory where ``turnup-check`` writes ``<site>-<date>.json``.
"""

from __future__ import annotations

try:
    from netbox.plugins import PluginConfig  # NetBox >= 3.7
except ImportError:  # pragma: no cover
    try:
        from extras.plugins import PluginConfig  # NetBox 3.5 / 3.6
    except ImportError:  # outside NetBox: keep the module importable for linting
        PluginConfig = object  # type: ignore[assignment,misc]

__version__ = "0.1.0"


class TurnupCheckConfig(PluginConfig):  # type: ignore[misc,valid-type]
    name = "netbox_turnup_check"
    verbose_name = "Turnup check"
    description = "Lists turnup-check reports: intended build versus what LLDP, Redfish and DCGM saw."
    version = __version__
    author = "Turnup contributors"
    author_email = ""
    base_url = "turnup-check"
    min_version = "3.5.0"
    required_settings: list[str] = []
    default_settings = {
        # Directory holding turnup.report/v1 JSON files written by `turnup-check --out`.
        "report_dir": "/var/lib/turnup/reports",
        # How many reports the list view shows, newest first.
        "max_reports": 200,
    }


config = TurnupCheckConfig
