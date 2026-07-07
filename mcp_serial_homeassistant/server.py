"""
MCP Serial HomeAssistant v1.0.7
Persistenter serieller Listener fuer ESP32-S2 USB-CDC.
FastMCP HTTP Transport.
Polling alle 0.5s (udev NETLINK in Docker geblockt).
ttyACM* hat immer Vorrang vor ttyUSB*.
Burst-Lines (CR-getrennt) werden in einzelne Eintraege aufgesplittet.
Monitor kann per MCP-Tool pausiert/fortgesetzt werden (z.B. fuer Flash-Vorgang).
Flash-Mode-Filter: ESP32-S2 im Download-Mode (VID:PID 303a:0002, USB JTAG/
Serial Debug Unit) wird NIE geoeffnet - nur normaler Betrieb (303a:4001 CDC)
oder unbekannte VID/PID werden verbunden.
"""
import logging
import os
import threading
import time
import glob
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import serial
import uvicorn
from fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp_serial")
for _noisy in ("uvicorn.access", "uvicorn.error", "mcp", "fastmcp"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

def load_options() -> dict:
    try:
        with open("/data/options.json") as f:
            return json.load(f)
    except Exception:
        return {}

opts = load_options()

state = {
    "baud_rate":           opts.get("baud_rate", 115200),
    "active_port":         None,
    "ring_buffer_lines":   opts.get("ring_buffer_lines", 300),
    "log_retention_hours": opts.get("log_retention_hours", 24),
    "log_max_size_mb":     opts.get("log_max_size_mb", 20),
    "monitor_active":      True,
    "monitor_paused_at":   None,
}
MCP_PORT = int(opts.get("port", 8769))

# VID:PID Kombinationen die NIE als Serial-Monitor-Ziel geoeffnet werden.
# 303a:0002 = Espressif USB JTAG/serial debug unit (Download/Flash-Mode)
BLOCKED_VID_PID = {
    ("303a", "0002"),
}

ring_buffer: deque = deque(maxlen=state["ring_buffer_lines"])
buffer_lock = threading.Lock()

LOG_DIR = Path("/data/serial_logs")
LOG_DIR.mkdir(exist_ok=True)
current_log_file: Path | None = None
log_lock = threading.Lock()

def get_log_path() -> Path:
    return LOG_DIR / (datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".log")

def rotate_logs():
    cutoff = time.time() - state["log_retention_hours"] * 3600
    for f in LOG_DIR.glob("*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except Exception:
            pass

def write_log_line(entry: dict):
    global current_log_file
    with log_lock:
        rotate_logs()
        if current_log_file is None:
            current_log_file = get_log_path()
        try:
            if current_log_file.exists() and \
               current_log_file.stat().st_size > state["log_max_size_mb"] * 1024 * 1024:
                current_log_file = get_log_path()
        except Exception:
            current_log_file = get_log_path()
        try:
            with open(current_log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.warning("Log write error: %s", e)

def split_and_clean(raw_line: str) -> list[str]:
    segments = raw_line.split("\r")
    result = []
    for seg in segments:
        seg = seg.strip()
        if seg:
            result.append(seg)
    return result

# ---------------------------------------------------------------------------
# USB VID/PID Erkennung ueber sysfs
# ---------------------------------------------------------------------------
def get_usb_vid_pid(tty_path: str) -> tuple[str, str] | None:
    """
    Ermittelt VID/PID des USB-Geraets hinter einem /dev/ttyACMx Node.
    Traversiert /sys/class/tty/ttyACMx/device nach oben bis idVendor/idProduct
    gefunden werden (max 6 Ebenen).
    """
    tty_name = os.path.basename(tty_path)
    sys_path = f"/sys/class/tty/{tty_name}/device"
    try:
        current = os.path.realpath(sys_path)
    except Exception:
        return None

    for _ in range(6):
        vendor_file = os.path.join(current, "idVendor")
        product_file = os.path.join(current, "idProduct")
        if os.path.exists(vendor_file) and os.path.exists(product_file):
            try:
                with open(vendor_file) as f:
                    vid = f.read().strip()
                with open(product_file) as f:
                    pid = f.read().strip()
                return (vid, pid)
            except Exception:
                return None
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None

def is_flash_mode(tty_path: str) -> bool:
    """True wenn das Geraet hinter tty_path im Flash/Download-Mode ist."""
    vid_pid = get_usb_vid_pid(tty_path)
    if vid_pid is None:
        return False
    return vid_pid in BLOCKED_VID_PID

def find_target_port() -> str | None:
    target = state["active_port"]
    if target:
        if not os.path.exists(target):
            return None
        if is_flash_mode(target):
            return None
        return target
    acm = sorted(glob.glob("/dev/ttyACM*"))
    for candidate in acm:
        if is_flash_mode(candidate):
            log.info("Ignoriere %s - Flash/Download-Mode erkannt (VID:PID 303a:0002)", candidate)
            continue
        return candidate
    return None

ser: serial.Serial | None = None
ser_lock = threading.Lock()

def open_port(port: str, baud: int) -> bool:
    global ser
    try:
        s = serial.Serial(port, baud, timeout=1, dsrdtr=False, rtscts=False)
        s.dtr = False
        s.rts = False
        with ser_lock:
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass
            ser = s
        log.info("Port geoeffnet: %s @ %d baud", port, baud)
        return True
    except Exception as e:
        log.debug("Open fehlgeschlagen %s: %s", port, e)
        return False

def close_port():
    global ser
    with ser_lock:
        if ser:
            try:
                ser.close()
            except Exception:
                pass
            ser = None

def store_line(port_name: str, line: str):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "port": port_name,
        "line": line,
    }
    with buffer_lock:
        ring_buffer.append(entry)
    write_log_line(entry)

def serial_loop():
    last_seen_port = None
    while True:
        if not state["monitor_active"]:
            close_port()
            last_seen_port = None
            time.sleep(0.5)
            continue

        with ser_lock:
            s = ser

        if s and s.is_open:
            # Laufend pruefen ob der offene Port evtl. jetzt Flash-Mode ist
            # (z.B. wenn ESP mitten im Betrieb in Download-Mode versetzt wird)
            if is_flash_mode(s.port):
                log.info("Port %s ist jetzt im Flash-Mode - trenne Verbindung", s.port)
                close_port()
                last_seen_port = None
                time.sleep(0.5)
                continue
            try:
                raw_bytes = s.readline()
                if raw_bytes:
                    decoded = raw_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                    for line in split_and_clean(decoded):
                        store_line(s.port, line)
            except serial.SerialException:
                log.info("Port verloren (ESP schlaeft) - polling...")
                close_port()
                last_seen_port = None
            except Exception as e:
                log.debug("Read error: %s", e)
                time.sleep(0.05)
        else:
            port = find_target_port()
            if port and port != last_seen_port:
                log.info("Port erkannt: %s - oeffne...", port)
                time.sleep(0.15)
                if open_port(port, state["baud_rate"]):
                    last_seen_port = port
                else:
                    last_seen_port = None
            elif not port:
                last_seen_port = None
            time.sleep(0.5)

# ---------------------------------------------------------------------------
# FastMCP Tools
# ---------------------------------------------------------------------------
mcp = FastMCP("MCP Serial HomeAssistant")

@mcp.tool()
def serial_monitor_stop() -> dict:
    """
    Pausiert den seriellen Monitor komplett. Kein Port wird geoeffnet oder gelesen.
    Verwenden vor Flash-Vorgang oder wenn der Port exklusiv benoetigt wird.
    Nach Add-on Neustart ist der Monitor automatisch wieder aktiv.
    """
    if not state["monitor_active"]:
        return {"monitor_active": False, "note": "War bereits pausiert", "paused_at": state["monitor_paused_at"]}
    close_port()
    state["monitor_active"] = False
    state["monitor_paused_at"] = datetime.now(timezone.utc).isoformat()
    log.info("Monitor PAUSIERT - kein Port wird geoeffnet")
    return {
        "monitor_active": False,
        "paused_at": state["monitor_paused_at"],
        "note": "Monitor pausiert. serial_monitor_start() zum Fortsetzen."
    }

@mcp.tool()
def serial_monitor_start() -> dict:
    """
    Startet den seriellen Monitor nach einer Pause wieder.
    Der Monitor erkennt automatisch den naechsten verfuegbaren ttyACM* Port
    (Geraete im Flash/Download-Mode werden dabei automatisch uebersprungen).
    """
    if state["monitor_active"]:
        return {"monitor_active": True, "note": "War bereits aktiv"}
    paused_at = state["monitor_paused_at"]
    state["monitor_active"] = True
    state["monitor_paused_at"] = None
    log.info("Monitor GESTARTET - Polling aktiv")
    return {
        "monitor_active": True,
        "was_paused_at": paused_at,
        "note": "Monitor aktiv. ttyACM* wird erkannt (Flash-Mode-Geraete werden ignoriert)."
    }

@mcp.tool()
def serial_read_recent(lines: int = 50) -> dict:
    """Letzte N Zeilen aus dem RAM-Ring-Buffer. Fuer schnelle Checks nach ESP32-S2 Aufwachen."""
    n = min(lines, state["ring_buffer_lines"])
    with buffer_lock:
        items = list(ring_buffer)[-n:]
    return {"lines": items, "count": len(items), "buffer_total": len(ring_buffer)}

@mcp.tool()
def serial_read_timerange(since: str = "", until: str = "", max_lines: int = 5000) -> dict:
    """Log-Eintraege aus Disk-Log fuer Zeitraum. since/until als ISO timestamp."""
    results = []
    for lf in sorted(LOG_DIR.glob("*.log")):
        try:
            with open(lf) as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except Exception:
                        continue
                    ts = entry.get("ts", "")
                    if since and ts < since:
                        continue
                    if until and ts > until:
                        continue
                    results.append(entry)
                    if len(results) >= max_lines:
                        break
        except Exception:
            continue
        if len(results) >= max_lines:
            break
    return {"lines": results, "count": len(results), "truncated": len(results) >= max_lines}

@mcp.tool()
def serial_list_ports() -> dict:
    """
    Zeigt alle verfuegbaren seriellen Ports (ttyACM*, ttyUSB*) inkl. by-id Symlinks
    und VID/PID sowie Flash-Mode-Status fuer jeden ttyACM* Port.
    """
    ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    by_id = {}
    try:
        for name in os.listdir("/dev/serial/by-id"):
            target = os.readlink(f"/dev/serial/by-id/{name}")
            by_id[name] = os.path.normpath(os.path.join("/dev/serial/by-id", target))
    except Exception:
        pass

    acm_details = {}
    for p in glob.glob("/dev/ttyACM*"):
        vid_pid = get_usb_vid_pid(p)
        acm_details[p] = {
            "vid_pid": f"{vid_pid[0]}:{vid_pid[1]}" if vid_pid else None,
            "flash_mode": is_flash_mode(p),
        }

    return {"ports": ports, "by_id": by_id, "acm_details": acm_details}

@mcp.tool()
def serial_set_port(port: str) -> dict:
    """Setzt den aktiven Port manuell. Leer-String = zurueck zu Auto-Detect."""
    if port == "":
        state["active_port"] = None
        return {"active_port": None, "note": "Auto-Detect aktiv"}
    state["active_port"] = port
    if not os.path.exists(port):
        return {"port": port, "opened": False, "note": "Existiert noch nicht, gilt beim naechsten Aufwachen"}
    if is_flash_mode(port):
        return {"port": port, "opened": False, "note": "Geraet ist im Flash/Download-Mode - wird nicht geoeffnet"}
    ok = open_port(port, state["baud_rate"])
    return {"port": port, "opened": ok}

@mcp.tool()
def serial_set_baudrate(baud_rate: int) -> dict:
    """Aendert die Baudrate live ohne Neustart. z.B. 115200, 9600, 921600"""
    state["baud_rate"] = baud_rate
    with ser_lock:
        s = ser
    if s and s.is_open:
        try:
            s.baudrate = baud_rate
            return {"baud_rate": baud_rate, "applied": True}
        except Exception as e:
            return {"baud_rate": baud_rate, "applied": False, "error": str(e)}
    return {"baud_rate": baud_rate, "applied": False, "note": "Kein Port offen, gilt beim naechsten Open"}

@mcp.tool()
def serial_status() -> dict:
    """Aktueller Status: Port, Baudrate, Monitor aktiv/pausiert, Buffer-Fuellstand, Log-Groesse."""
    with ser_lock:
        s = ser
    log_files = list(LOG_DIR.glob("*.log"))
    with buffer_lock:
        buf_len = len(ring_buffer)
    return {
        "monitor_active": state["monitor_active"],
        "paused_at": state["monitor_paused_at"],
        "port": s.port if s else None,
        "configured_port": state["active_port"],
        "auto_detect": state["active_port"] is None,
        "baud_rate": state["baud_rate"],
        "port_open": s.is_open if s else False,
        "ring_buffer_used": buf_len,
        "ring_buffer_capacity": state["ring_buffer_lines"],
        "log_files": len(log_files),
        "log_size_bytes": sum(f.stat().st_size for f in log_files),
    }

if __name__ == "__main__":
    threading.Thread(target=serial_loop, daemon=True).start()
    log.info("MCP Serial HomeAssistant v1.0.7 gestartet auf Port %d", MCP_PORT)
    log.info("Auto-Detect: ttyACM* bevorzugt, Burst-Splitter aktiv, Flash-Mode-Filter aktiv (303a:0002 blockiert)")
    app = mcp.http_app()
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="warning")
