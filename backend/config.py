import yaml
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
_CONFIG_PATH = BASE_DIR / "config.yaml"


def load_config() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


class _LazyConfig(dict):
    """Dict that re-reads config.yaml on every access so restarts aren't needed."""
    def _reload(self):
        self.clear()
        self.update(load_config())

    def get(self, key, default=None):
        self._reload()
        return super().get(key, default)

    def __getitem__(self, key):
        self._reload()
        return super().__getitem__(key)

    def __contains__(self, key):
        self._reload()
        return super().__contains__(key)


config = _LazyConfig(load_config())
