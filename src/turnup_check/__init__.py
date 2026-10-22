# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""turnup-check: diff the intended GPU-hall build against what LLDP, Redfish and DCGM see."""

__version__ = "0.1.0"
TOOL_NAME = "turnup-check"
INTENT_SCHEMA = "turnup.intent/v1"
REPORT_SCHEMA = "turnup.report/v1"
PING_SCHEMA = "turnup.ping/v1"

__all__ = ["INTENT_SCHEMA", "PING_SCHEMA", "REPORT_SCHEMA", "TOOL_NAME", "__version__"]
