"""Read-only serial device discovery and chip identification via
esptool/pyserial. Complements serial_flash.py (which writes firmware);
nothing here ever writes to flash.

CAUTION (confirmed by direct testing earlier in this addon's
development): esptool's own chip-detection handshake ends with a reset
of the target ("Hard resetting via RTS pin..."). For ESP32-S2/S3 boards
with native USB-CDC (no external USB-serial chip), any reset can make
the port vanish and re-enumerate on an unpredictable timescale (seen
ranging from minutes to hours) -- the same root cause documented in
serial_flash.py for why ESPHome's own upload mechanism doesn't work on
these boards. Calling get_chip_info() on such a board WILL likely knock
it out of flashing mode; the caller should expect to need to
re-trigger flashing mode afterward if they intend to flash next.
"""
import asyncio
import logging
import re
from typing import Optional

try:
    import serial.tools.list_ports as list_ports
    HAS_PYSERIAL = True
except ImportError:
    HAS_PYSERIAL = False

_LOGGER = logging.getLogger("mcp_esphome.serial_info")

_CHIP_TYPE_RE = re.compile(r"Chip type:\s+(.+)")
_FEATURES_RE = re.compile(r"Features:\s+(.+)")
_CRYSTAL_RE = re.compile(r"Crystal frequency:\s+(.+)")
_USB_MODE_RE = re.compile(r"USB mode:\s+(.+)")
_MAC_RE = re.compile(r"^MAC:\s+([0-9a-fA-F:]+)", re.MULTILINE)
_PSRAM_RE = re.compile(r"Embedded PSRAM ([\w.]+\s*\w*)")
_FLASH_RE = re.compile(r"Embedded Flash ([\w.]+\s*\w*)")


def list_serial_ports() -> list[dict]:
    """Lists serial devices currently visible to THIS container (needs
    uart: true in config.yaml, already set). Includes non-ESP serial
    devices too (e.g. Zigbee dongles) so the caller can tell them apart
    by description/VID/PID before picking a port to query or flash.
    """
    if not HAS_PYSERIAL:
        return []
    result = []
    for p in list_ports.comports():
        result.append({
            "port": p.device,
            "description": p.description,
            "vid": p.vid,
            "pid": p.pid,
        })
    return result


def _parse_chip_info(output: str) -> dict:
    info: dict = {"raw_output": output}

    m = _CHIP_TYPE_RE.search(output)
    info["chip_type"] = m.group(1).strip() if m else None

    m = _FEATURES_RE.search(output)
    features_str = m.group(1).strip() if m else ""
    info["features"] = [f.strip() for f in features_str.split(",")] if features_str else []

    m = _PSRAM_RE.search(features_str)
    info["embedded_psram"] = m.group(1).strip() if m else None

    m = _FLASH_RE.search(features_str)
    info["embedded_flash"] = m.group(1).strip() if m else None

    m = _CRYSTAL_RE.search(output)
    info["crystal_frequency"] = m.group(1).strip() if m else None

    m = _USB_MODE_RE.search(output)
    info["usb_mode"] = m.group(1).strip() if m else None

    m = _MAC_RE.search(output)
    info["mac_address"] = m.group(1).strip() if m else None

    return info


async def get_chip_info(port: str) -> dict:
    """Runs esptool's chip-detection handshake (read-only: no flash
    write) and returns parsed chip details -- type/revision, feature
    list, embedded flash size, embedded PSRAM size (None if the chip
    has none), crystal frequency, USB mode, and MAC address.

    WARNING: this resets the target at the end (esptool's own
    behavior). On ESP32-S2/S3 native-USB boards this can knock the
    device out of flashing mode for an unpredictable time -- see
    module docstring. Only call this when you actually need the chip
    info, not as a routine pre-flash check.
    """
    cmd = ["esptool", "--port", port, "chip-id"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace")
    exit_code = proc.returncode

    if exit_code != 0:
        return {"success": False, "exit_code": exit_code, "output": output}

    info = _parse_chip_info(output)
    info["success"] = True
    info["exit_code"] = exit_code
    return info
