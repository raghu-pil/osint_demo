import yaml
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent


def load_config() -> dict:
    config_path = BASE_DIR / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


config = load_config()
