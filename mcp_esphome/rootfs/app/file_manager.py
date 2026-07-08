"""File Manager - generic read/write/list access to the ESPHome config
tree (/config/esphome/), NOT limited to device YAMLs.

Covers everything ESPHome itself can read from that directory: device
configs, package/template files (e.g. ZZVorlageDeepSleepSettingsV2.yaml,
included elsewhere via !include), and custom components
(components/<name>/__init__.py, *.py per platform, *.cpp, *.h) -- so
Claude can actually edit configs and custom component code, not just
operate on already-existing, already-working devices.

All paths are given relative to /config/esphome/ and resolved safely
(blocking any path traversal outside that root, e.g. "../../etc/passwd")
before touching disk. Confirmed by direct testing (read template, read
custom component .py, list a directory, write+read+delete a scratch
file, and a blocked traversal attempt) before being wired into the API.
"""
import logging
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger("mcp_esphome.file_manager")

ESPHOME_CONFIG_DIR = Path("/config/esphome")


def _resolve_safe(relative_path: str) -> Path:
    """Resolves relative_path against ESPHOME_CONFIG_DIR. Raises
    ValueError if the resolved path would land outside that root
    (blocks '../' traversal).
    """
    candidate = (ESPHOME_CONFIG_DIR / relative_path).resolve()
    root = ESPHOME_CONFIG_DIR.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"'{relative_path}' resolves outside {root}")
    return candidate


def list_files(relative_path: str = "") -> dict:
    """Lists the immediate contents of a directory under
    /config/esphome/. Use this to explore templates/ and components/
    subtrees before reading/writing specific files.
    """
    try:
        path = _resolve_safe(relative_path)
    except ValueError as err:
        return {"error": str(err)}
    if not path.exists():
        return {"error": f"{relative_path!r} does not exist"}
    if not path.is_dir():
        return {"error": f"{relative_path!r} is not a directory"}
    entries = []
    for entry in sorted(path.iterdir()):
        entries.append({
            "name": entry.name,
            "is_dir": entry.is_dir(),
            "size": entry.stat().st_size if entry.is_file() else None,
        })
    return {"path": relative_path, "entries": entries}


def read_file(relative_path: str) -> dict:
    """Reads a text file (YAML, .py, .cpp, .h, .txt, ...) under
    /config/esphome/. Fails cleanly (no crash) on binary files that
    aren't valid UTF-8.
    """
    try:
        path = _resolve_safe(relative_path)
    except ValueError as err:
        return {"error": str(err)}
    if not path.exists():
        return {"error": f"{relative_path!r} does not exist"}
    if path.is_dir():
        return {"error": f"{relative_path!r} is a directory, not a file"}
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"error": f"{relative_path!r} is not a UTF-8 text file (binary?)"}
    except Exception as err:
        return {"error": f"Could not read {relative_path!r}: {err}"}
    return {"path": relative_path, "content": content, "size": len(content)}


def write_file(relative_path: str, content: str, create_dirs: bool = False) -> dict:
    """Writes (overwrites, or creates) a text file under
    /config/esphome/. Parent directories must already exist unless
    create_dirs=True -- required for genuinely new custom components
    or template files, since their directories won't exist yet.
    """
    try:
        path = _resolve_safe(relative_path)
    except ValueError as err:
        return {"error": str(err)}
    if not path.parent.exists():
        if not create_dirs:
            return {
                "error": (
                    f"Parent directory of {relative_path!r} does not exist. "
                    "Pass create_dirs=true to create it."
                )
            }
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content, encoding="utf-8")
    except Exception as err:
        return {"error": f"Could not write {relative_path!r}: {err}"}
    _LOGGER.info("Wrote %d bytes to %s", len(content), path)
    return {"path": relative_path, "bytes_written": len(content)}
