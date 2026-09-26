from __future__ import annotations

"""Read-only shadow probe for JEV Agent Recovery Watchdog v0.1.

This module accepts only explicit disposable War Room test sessions. It reads
chat history, builds evidence, asks JEV for an advisory decision, and appends a
shadow record. It never sends agent messages, creates sessions, stops runs, or
invokes RecoveryController.
"""

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from jev_agent_recovery_watchdog_v01 import (
    AgentActivity,
    Availability,
    ExistingOpenConnectorHealthJudge,
    FeatureBuilder,
    ObserverSnapshot,
)
from war_room_adapter import PersistentGatewayBridge


JEV_KEYS = {
    "JEV_OPENCONNECTOR_ENDPOINT",
    "JEV_OPENCONNECTOR_CONNECTION",
    "JEV_OPENCONNECTOR_TOKEN_FILE",
}
