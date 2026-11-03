"""Loads a client profile from configs/<profile>/.

Client-specific values live in those files, not in code. Each component
reads only its own section.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CONFIGS = Path(__file__).resolve().parents[3] / "configs"


def load_settings(profile: str = "demo", configs: Path = CONFIGS) -> dict:
    with open(configs / profile / "settings.yaml") as f:
        return yaml.safe_load(f) or {}


def load_rules(profile: str = "demo", configs: Path = CONFIGS) -> dict:
    with open(configs / profile / "rules.yaml") as f:
        return yaml.safe_load(f) or {}
