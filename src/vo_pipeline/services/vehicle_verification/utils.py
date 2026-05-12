# ================================================================================
# FILE: utils.py
# ================================================================================
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from matching import normalize_for_vin_match, normalize_for_odometer_match  # noqa: F401

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp", "heic", "svg"}
UNSUPPORTED_FOR_VLM = {"heic", "svg", "bmp", "tiff", "tif"}
DEFAULT_MIN_TEXT_LENGTH = 4


# ── Config reader (generic, reusable across all modules) ─────────────────────


def read_config(config_path: str) -> Dict[str, Any]:
    """
    Load a YAML config file and return its contents as a plain dict.

    Args:
        config_path: Path (str or Path-like) to the YAML file.

    Raises:
        FileNotFoundError: if the file does not exist.
        yaml.YAMLError:    if the file is not valid YAML.
    """
    import yaml  # PyYAML — present in the project's dependencies

    path = Path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path.resolve()}")

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    return data if isinstance(data, dict) else {}


# ── File-name helpers ─────────────────────────────────────────────────────────


def is_image_name(name: str) -> bool:
    return name.lower().split(".")[-1] in IMAGE_EXTS


def is_pdf_name(name: str) -> bool:
    return name.lower().endswith(".pdf")


def is_thumbnail_name(name: str, thumb_key: str) -> bool:
    return thumb_key.lower() in Path(name).name.lower()


def leaf_name_of_prefix(prefix: str) -> str:
    p = prefix.rstrip("/").split("/")
    return p[-1] if p else prefix.rstrip("/")


# ── Canonical home: matching.py – re-exported for backward compatibility ──────

# ── Misc value helpers ────────────────────────────────────────────────────────


def odo_to_str(val: Any) -> str:
    if val is None:
        return ""
    try:
        return str(int(float(val)))
    except (ValueError, TypeError):
        return str(val)
