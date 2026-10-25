"""
Desktop MQTT Agent - publishes the status of a Windows PC to Home Assistant over MQTT.

Entities appear in Home Assistant automatically through MQTT discovery
(one device with sensors, binary sensors and buttons).

Usage:
    pythonw desktop_mqtt_agent.py            # run normally (no console window)
    python  desktop_mqtt_agent.py --dry-run  # print one reading + discovery, don't connect
"""

import argparse
import configparser
import ctypes
import json
import logging
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psutil

APP_NAME = "Desktop MQTT Agent"
APP_SLUG = "desktop_mqtt_agent"
VERSION = "1.2.0"
BASE_DIR = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform == "win32"

# Identifiers from before the rename. They stay as they are, so existing
# installations keep their Home Assistant device and their stored password.
LEGACY_DEVICE_PREFIX = "ha_pc_agent_"
LEGACY_KEYRING_SERVICE = "ha_pc_agent"
KEYRING_SERVICE = APP_SLUG

log = logging.getLogger(APP_SLUG)

# Entity names shown in Home Assistant, per language ([device] language = en|nl).
NAMES = {
    "en": {
        "cpu": "CPU usage", "ram": "Memory usage", "disk": "Disk C: usage",
        "idle": "Idle", "boot": "Last boot", "app": "Active app",
        "locked": "Locked", "in_use": "In use",
        "lock_btn": "Lock", "sleep_btn": "Sleep", "shutdown_btn": "Shut down",
        "restart_btn": "Restart",
        "cpu_temp": "CPU temperature", "gpu_temp": "GPU temperature", "gpu_load": "GPU usage",
        "model": "Windows PC",
        "shutdown_msg": "Shut down from Home Assistant",
        "restart_msg": "Restarted from Home Assistant",
    },
    "nl": {
        "cpu": "CPU-gebruik", "ram": "Geheugengebruik", "disk": "Schijf C: gebruik",
        "idle": "Inactief", "boot": "Opgestart", "app": "Actieve app",
        "locked": "Vergrendeld", "in_use": "In gebruik",
        "lock_btn": "Vergrendelen", "sleep_btn": "Slaapstand", "shutdown_btn": "Afsluiten",
        "restart_btn": "Herstarten",
        "cpu_temp": "CPU-temperatuur", "gpu_temp": "GPU-temperatuur", "gpu_load": "GPU-gebruik",
        "model": "Windows-pc",
        "shutdown_msg": "Afgesloten via Home Assistant",
        "restart_msg": "Herstart via Home Assistant",
    },
}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({
        "mqtt": {"host": "", "port": "1883", "username": "", "password": "",
                 "discovery_prefix": "homeassistant", "keepalive": "30"},
        "device": {"name": socket.gethostname(), "id": "", "language": "en"},
        "agent": {"interval": "15", "in_use_idle_minutes": "5",
                  "allow_power_commands": "true", "shutdown_delay": "5"},
        "lhm": {"enabled": "false", "url": "http://localhost:8085/data.json",
                "cpu_temp": "Temperatures/Core (Tctl/Tdie)",
                "gpu_temp": "Temperatures/GPU Core",
                "gpu_load": "Load/GPU Core"},
    })
    if path.exists():
        cfg.read(path, encoding="utf-8-sig")  # -sig: PowerShell sometimes writes a BOM
    return cfg


# --------------------------------------------------------------------------- #
# Windows readings
# --------------------------------------------------------------------------- #
class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint32)]


def idle_seconds():
    """Seconds since the last mouse or keyboard input."""
    if not IS_WINDOWS:
        return None
    lii = _LastInputInfo()
    lii.cbSize = ctypes.sizeof(lii)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.GetTickCount.restype = ctypes.c_uint32
    millis = (kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
    return int(millis / 1000)


def is_locked():
    """Is the lock screen shown? (LogonUI.exe runs while it is.)"""
    if not IS_WINDOWS:
        return None
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() == "logonui.exe":
            return True
    return False


def active_app():
    """Process name of the foreground window (not the window title, for privacy)."""
    if not IS_WINDOWS:
        return None
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def system_drive_usage():
    path = "C:\\" if IS_WINDOWS else "/"
    try:
        return round(psutil.disk_usage(path).percent, 1)
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# LibreHardwareMonitor (optional, for temperatures and GPU)
# --------------------------------------------------------------------------- #
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def _walk_lhm(node, path, found):
    text = node.get("Text", "")
    new_path = path + [text]
    children = node.get("Children") or []
    if not children and node.get("Value"):
        found.append(("/".join(new_path), node["Value"]))
    for child in children:
        _walk_lhm(child, new_path, found)


def read_lhm(url, wanted):
    """Returns {key: value} for every path suffix in `wanted`."""
    result = {key: None for key in wanted}
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            tree = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - LHM is optional, never crash on it
        log.debug("LibreHardwareMonitor not reachable: %s", exc)
        return result
    leaves = []
    _walk_lhm(tree, [], leaves)
    for key, suffix in wanted.items():
        for leaf_path, value in leaves:
            if leaf_path.endswith(suffix):
                match = _NUMBER.search(value)
                if match:
                    result[key] = float(match.group().replace(",", "."))
                break
    return result


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class Agent:
    def __init__(self, cfg: configparser.ConfigParser):
        self.cfg = cfg
        self.interval = cfg.getint("agent", "interval")
        self.in_use_idle = cfg.getint("agent", "in_use_idle_minutes") * 60
        self.allow_power = cfg.getboolean("agent", "allow_power_commands")
        self.lhm_enabled = cfg.getboolean("lhm", "enabled")

        lang = cfg.get("device", "language").strip().lower()
        if lang not in NAMES:
            log.warning("Unknown language '%s', using English", lang)
            lang = "en"
        self.t = NAMES[lang]

        self.device_name = cfg.get("device", "name")
        self.device_id = cfg.get("device", "id") or re.sub(
            r"[^a-z0-9_]", "_", socket.gethostname().lower())
        self.prefix = cfg.get("mqtt", "discovery_prefix")

        base = f"{APP_SLUG}/{self.device_id}"
        self.state_topic = f"{base}/state"
        self.avail_topic = f"{base}/availability"
        self.command_topic = f"{base}/command"
        self.discovery_topic = f"{self.prefix}/device/{self.device_id}/config"

        self.client = None
        self.suspended_at = None
        self.stop_event = threading.Event()
        psutil.cpu_percent(interval=None)  # prime the first CPU reading

    # ---- discovery ------------------------------------------------------- #
    def _sensor(self, key, **extra):
        comp = {
            "p": "sensor",
            "name": self.t[key],
            "unique_id": f"{self.device_id}_{key}",
            "state_topic": self.state_topic,
            "value_template": "{{ value_json.%s }}" % key,
        }
        comp.update(extra)
        return comp

    def _button(self, key, payload, icon):
        return {
            "p": "button", "name": self.t[key],
            "unique_id": f"{self.device_id}_{key}",
            "command_topic": self.command_topic, "payload_press": payload,
            "icon": icon,
        }

    def discovery_payload(self):
        cmps = {
            "cpu": self._sensor("cpu", unit_of_measurement="%",
                                state_class="measurement", icon="mdi:cpu-64-bit"),
            "ram": self._sensor("ram", unit_of_measurement="%",
                                state_class="measurement", icon="mdi:memory"),
            "disk": self._sensor("disk", unit_of_measurement="%",
                                 state_class="measurement", icon="mdi:harddisk",
                                 entity_category="diagnostic"),
            "idle": self._sensor("idle", unit_of_measurement="s",
                                 device_class="duration", state_class="measurement"),
            "boot": self._sensor("boot", device_class="timestamp",
                                 entity_category="diagnostic"),
            "app": self._sensor("app", icon="mdi:application"),
            "locked": {
                "p": "binary_sensor", "name": self.t["locked"],
                "unique_id": f"{self.device_id}_locked",
                "state_topic": self.state_topic,
                "value_template": "{{ 'ON' if value_json.locked else 'OFF' }}",
                "icon": "mdi:monitor-lock",
            },
            "in_use": {
                "p": "binary_sensor", "name": self.t["in_use"],
                "unique_id": f"{self.device_id}_in_use",
                "state_topic": self.state_topic,
                "value_template": "{{ 'ON' if value_json.in_use else 'OFF' }}",
                "device_class": "occupancy",
            },
            "lock_btn": self._button("lock_btn", "lock", "mdi:lock"),
        }
        if self.allow_power:
            cmps["sleep_btn"] = self._button("sleep_btn", "sleep", "mdi:sleep")
            cmps["shutdown_btn"] = self._button("shutdown_btn", "shutdown", "mdi:power")
            cmps["restart_btn"] = self._button("restart_btn", "restart", "mdi:restart")
        if self.lhm_enabled:
            cmps["cpu_temp"] = self._sensor("cpu_temp", unit_of_measurement="°C",
                                            device_class="temperature",
                                            state_class="measurement")
            cmps["gpu_temp"] = self._sensor("gpu_temp", unit_of_measurement="°C",
                                            device_class="temperature",
                                            state_class="measurement")
            cmps["gpu_load"] = self._sensor("gpu_load", unit_of_measurement="%",
                                            state_class="measurement",
                                            icon="mdi:expansion-card")
        return {
            "dev": {"ids": [f"{LEGACY_DEVICE_PREFIX}{self.device_id}"],
                    "name": self.device_name,
                    "mf": APP_NAME, "mdl": self.t["model"], "sw": VERSION},
            "o": {"name": APP_SLUG, "sw": VERSION},
            "availability_topic": self.avail_topic,
            "qos": 1,
            "cmps": cmps,
        }

    # ---- state ----------------------------------------------------------- #
    def collect_state(self):
        idle = idle_seconds()
        locked = is_locked()
        state = {
            "cpu": round(psutil.cpu_percent(interval=None), 1),
            "ram": round(psutil.virtual_memory().percent, 1),
            "disk": system_drive_usage(),
            "idle": idle,
            "boot": datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc).isoformat(),
            "app": active_app(),
            "locked": bool(locked),
            "in_use": bool(idle is not None and idle < self.in_use_idle and not locked),
        }
        if self.lhm_enabled:
            state.update(read_lhm(self.cfg.get("lhm", "url"), {
                "cpu_temp": self.cfg.get("lhm", "cpu_temp"),
                "gpu_temp": self.cfg.get("lhm", "gpu_temp"),
                "gpu_load": self.cfg.get("lhm", "gpu_load"),
            }))
        return state

    # ---- commands -------------------------------------------------------- #
    def _go_offline(self):
        """Report offline right away, so HA doesn't wait for the keepalive timeout."""
        self.suspended_at = time.time()
        info = self.client.publish(self.avail_topic, "offline", qos=1, retain=True)
        info.wait_for_publish(timeout=3)

    def handle_command(self, payload: str):
        log.info("Command received: %s", payload)
        if payload == "lock":
            ctypes.windll.user32.LockWorkStation()
            return
        if not self.allow_power:
            log.warning("Power command '%s' refused (allow_power_commands = false)", payload)
            return
        delay = str(self.cfg.getint("agent", "shutdown_delay"))
        if payload == "sleep":
            self._go_offline()
            # Note: with hibernation enabled, Windows hibernates instead of sleeping.
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        elif payload == "shutdown":
            self._go_offline()
            subprocess.Popen(["shutdown", "/s", "/t", delay, "/c", self.t["shutdown_msg"]])
        elif payload == "restart":
            self._go_offline()
            subprocess.Popen(["shutdown", "/r", "/t", delay, "/c", self.t["restart_msg"]])
        else:
            log.warning("Unknown command: %s", payload)

    # ---- MQTT ------------------------------------------------------------ #
    def _publish_discovery(self, client):
        client.publish(self.discovery_topic, json.dumps(self.discovery_payload()),
                       qos=1, retain=True)
        client.publish(self.avail_topic, "online", qos=1, retain=True)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("Connected to MQTT broker")
        self._publish_discovery(client)
        client.subscribe(self.command_topic, qos=1)
        client.subscribe(f"{self.prefix}/status", qos=1)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        if msg.topic == f"{self.prefix}/status":
            if payload == "online":  # HA restarted: send discovery again
                log.info("Home Assistant online, discovery published again")
                self._publish_discovery(client)
            return
        if msg.topic == self.command_topic:
            try:
                self.handle_command(payload)
            except Exception:  # noqa: BLE001
                log.exception("Command '%s' failed", payload)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        log.warning("Disconnected from MQTT broker (%s); paho reconnects automatically",
                    reason_code)

    def run(self):
        import paho.mqtt.client as mqtt

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"{APP_SLUG}_{self.device_id}")
        user = self.cfg.get("mqtt", "username")
        if user:
            self.client.username_pw_set(user, get_password(self.cfg))
        self.client.will_set(self.avail_topic, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(min_delay=2, max_delay=60)

        host = self.cfg.get("mqtt", "host")
        port = self.cfg.getint("mqtt", "port")
        keepalive = self.cfg.getint("mqtt", "keepalive")

        # Keep trying until the broker is reachable (e.g. network not ready right after logon).
        while not self.stop_event.is_set():
            try:
                self.client.connect(host, port, keepalive)
                break
            except OSError as exc:
                log.warning("Broker %s:%s not reachable (%s), retrying in 15 s", host, port, exc)
                self.stop_event.wait(15)

        self.client.loop_start()
        last_tick = time.time()
        try:
            while not self.stop_event.is_set():
                now = time.time()
                # A gap in time means the PC has been asleep.
                woke_up = now - last_tick > self.interval + 20
                last_tick = now
                if woke_up:
                    log.info("PC woke up from sleep")
                # After a sleep command, send nothing until the PC has actually slept
                # (or, if sleep didn't happen, carry on after 2 minutes).
                if self.suspended_at and (woke_up or now - self.suspended_at > 120):
                    self.suspended_at = None
                if self.client.is_connected() and not self.suspended_at:
                    try:
                        # Report "online" every round: this also restores the status when
                        # the connection survived sleep (no new on_connect).
                        self.client.publish(self.avail_topic, "online", qos=1, retain=True)
                        self.client.publish(self.state_topic,
                                            json.dumps(self.collect_state()), qos=0)
                    except Exception:  # noqa: BLE001
                        log.exception("Reading or sending the status failed")
                self.stop_event.wait(self.interval)
        except KeyboardInterrupt:
            pass
        finally:
            if self.client.is_connected():
                self._go_offline()
            self.client.loop_stop()
            self.client.disconnect()


# --------------------------------------------------------------------------- #
# Password in Windows Credential Manager (via keyring)
# --------------------------------------------------------------------------- #
def get_password(cfg):
    """Password from Windows Credential Manager; config.ini only as a fallback."""
    plain = cfg.get("mqtt", "password")
    if plain:
        return plain
    user = cfg.get("mqtt", "username")
    try:
        import keyring
        password = keyring.get_password(KEYRING_SERVICE, user)
        if password:
            return password
        # Installed before the rename? Take the password over from the old entry.
        password = keyring.get_password(LEGACY_KEYRING_SERVICE, user)
        if password:
            keyring.set_password(KEYRING_SERVICE, user, password)
            log.info("Password taken over from the old Credential Manager entry")
            return password
        return ""
    except Exception as exc:  # noqa: BLE001
        log.error("Could not read the password from Credential Manager: %s", exc)
        return ""


def migrate_password(cfg, config_path: Path):
    """Is the password still readable in config.ini? Move it to Credential Manager."""
    plain = cfg.get("mqtt", "password")
    if not plain or not config_path.exists():
        return
    user = cfg.get("mqtt", "username")
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, user, plain)
        if keyring.get_password(KEYRING_SERVICE, user) != plain:
            raise RuntimeError("check after saving failed")
    except Exception as exc:  # noqa: BLE001
        log.warning("Password not moved to Credential Manager (%s); it stays in config.ini", exc)
        return
    text = config_path.read_text(encoding="utf-8-sig")
    text = re.sub(r"(?m)^password\s*=.*$", "password =", text)
    config_path.write_text(text, encoding="utf-8")
    cfg.set("mqtt", "password", "")
    log.info("Password moved to Windows Credential Manager and removed from config.ini")


def main():
    parser = argparse.ArgumentParser(description="Publishes PC status to Home Assistant over MQTT")
    parser.add_argument("--config", default=str(BASE_DIR / "config.ini"))
    parser.add_argument("--dry-run", action="store_true",
                        help="print one reading and the discovery message without connecting")
    args = parser.parse_args()

    log_file = BASE_DIR / f"{APP_SLUG}.log"
    handlers = [logging.FileHandler(log_file, encoding="utf-8")]
    if sys.stdout:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    cfg = load_config(Path(args.config))
    agent = Agent(cfg)

    if args.dry_run:
        time.sleep(1)
        print("Discovery topic:", agent.discovery_topic)
        print(json.dumps(agent.discovery_payload(), indent=2, ensure_ascii=False))
        print("\nState topic:", agent.state_topic)
        print(json.dumps(agent.collect_state(), indent=2, ensure_ascii=False))
        return

    if not cfg.get("mqtt", "host"):
        log.error("No MQTT host set in %s", args.config)
        sys.exit(1)
    migrate_password(cfg, Path(args.config))
    agent.run()


if __name__ == "__main__":
    main()
