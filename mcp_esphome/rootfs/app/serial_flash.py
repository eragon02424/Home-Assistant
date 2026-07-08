"""Serial flashing via esptool subprocess, bypassing ESPHome's own
upload mechanism entirely.

WHY THIS EXISTS: ESPHome's own serial upload resets the device before
writing (needed to enter the ROM bootloader), and for ESP32-S2/S3
boards with native USB-CDC (no external USB-serial chip) that reset
makes the whole port re-enumerate mid-flash and vanish -- confirmed by
direct testing (dmesg showed a genuine "USB disconnect" with the port
gone for anywhere from minutes to hours afterward, requiring the user
to manually re-enter flashing mode). Source of the fix:
github.com/esphome/issues/issues/4090 -- flash with a tool that passes
--before no-reset, so it never triggers that reset in the first place.

esptool itself already does the RIGHT reset sequence when ENTERING the
bootloader for its own sync handshake (confirmed working reliably in
this same troubleshooting session); --before no-reset only means it
does NOT do an *additional* reset before starting that sync -- letting
the board's existing flashing-mode state (however the user put it
there) survive untouched.

PREREQUISITE: the compiled firmware.factory.bin must be reachable from
THIS container. By default it isn't (ESPHome's build output lives in
the ESPHome addon's own private /data volume). device_manager.py's
ensure_build_path() redirects the build via ESPHome's own build_path
YAML option to /config/esphome/.build/<name>, which IS mapped into
this addon (map: config:rw) -- confirmed by direct testing: the
compiled firmware.factory.bin (single merged image: bootloader +
partitions + app at their correct offsets) appears exactly where
expected after a normal compile.

CONFIRMED SEPARATELY (v0.18.0 first live test): the port frequently
does not exist at all by the time this runs, even at the HOST level
(not just inside this container) -- the same "port vanishes after any
reset trigger, comes back after an unpredictable delay (minutes to
hours), only reliably via the user manually re-entering flashing mode"
behavior documented earlier in this troubleshooting session. This
function can only flash a port that currently exists; it cannot force
the physical device back onto the USB bus.
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger("mcp_esphome.serial_flash")


async def flash_factory_bin(bin_path: Path, port: str, baud: int = 115200) -> dict:
    """Runs esptool as a subprocess to write a single merged
    firmware.factory.bin at flash offset 0x0, with --before no-reset so
    esptool never resets the port itself before starting -- whatever
    state the user put the board in (flashing mode) is left alone.
    esptool auto-detects the chip type, so no --chip flag is needed.

    Returns {"success": bool, "exit_code": int, "output": str}.
    Raises FileNotFoundError if bin_path doesn't exist (caller should
    check job status / trigger a compile first).
    """
    if not bin_path.exists():
        raise FileNotFoundError(
            f"{bin_path} does not exist — compile the device first "
            "(with ensure_build_path() applied) before flashing."
        )

    cmd = [
        "esptool",
        "--port", port,
        "--baud", str(baud),
        "--before", "no-reset",
        "--after", "hard-reset",
        "write-flash",
        "-z",
        "--flash-size", "detect",
        "0x0", str(bin_path),
    ]
    _LOGGER.info("Running: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace")
    exit_code = proc.returncode

    _LOGGER.info("esptool finished: exit_code=%s", exit_code)
    return {"success": exit_code == 0, "exit_code": exit_code, "output": output}
