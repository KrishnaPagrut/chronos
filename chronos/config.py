"""Environment, filesystem paths, and shared configuration.

Everything that reads the outside world (``.env``, disk paths) lives here so the
rest of the package can import a single, side-effect-light module. Note that
``pairing`` deliberately does *not* import this — it stays pure and dependency-free.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEMO_MODE = os.getenv("CHRONOS_DEMO_ONLY", "").lower() in {"1", "true", "yes"}
# Local pipeline state stays ignored in ``data/``. The production explorer uses
# the deliberately curated, committed ``demo_data/`` bundle instead.
DATA_DIR = ROOT / ("demo_data" if DEMO_MODE else "data")
IMAGES_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "chronos.db"
PROMPTS_DIR = ROOT / "prompts"

# Load local credentials only for the pipeline/development server. The deployed
# explorer must neither read nor expose them, even if a developer has a .env.
if not DEMO_MODE:
    load_dotenv(ROOT / ".env")

MAPILLARY_TOKEN = "" if DEMO_MODE else os.getenv("MAPILLARY_TOKEN", "")
OPENAI_API_KEY = "" if DEMO_MODE else os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
BRIEF_MODEL = os.getenv("BRIEF_MODEL", "gpt-5.6-terra")


def ensure_dirs() -> None:
    """Create the runtime data directories (idempotent)."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def thumb_path(image_id: str, size: int) -> Path:
    """Local cache path for an image's thumbnail at a given pixel size."""
    return IMAGES_DIR / f"{image_id}_{size}.jpg"
