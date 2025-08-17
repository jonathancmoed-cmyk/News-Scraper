from pathlib import Path
import yaml

# Base = repo root
BASE_DIR = Path(__file__).resolve().parents[1]

# Folders for outputs + cache
OUTPUT_DIR = BASE_DIR / "output"
CACHE_DIR = BASE_DIR / "cache"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Path to feeds.yaml
FEEDS_PATH = BASE_DIR / "config" / "feeds.yaml"

def load_feeds(path: Path | None = None):
    """
    Load feeds.yaml and return list of feed configs.
    """
    p = Path(path) if path else FEEDS_PATH
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("feeds", [])
