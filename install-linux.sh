#!/usr/bin/env bash
# Desktop MQTT Agent - installer for Linux (tested with Fedora Atomic / Bazzite, KDE Plasma).
#
# Installs the agent in ~/.local/share/desktop-mqtt-agent, asks for the MQTT
# settings and runs it as a systemd user service that starts at every login.
# No sudo needed.
#
#   bash install-linux.sh             install or update
#   bash install-linux.sh --uninstall remove the service and the installed files
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/desktop-mqtt-agent"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="desktop-mqtt-agent.service"

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT"
    systemctl --user daemon-reload
    rm -rf "$APP_DIR"
    echo "Removed. The device stays in Home Assistant until you delete it there."
    exit 0
fi

command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }

echo "== Desktop MQTT Agent: Linux installation =="
mkdir -p "$APP_DIR" "$UNIT_DIR"
cp "$SRC_DIR/desktop_mqtt_agent.py" "$APP_DIR/"

# Python packages in their own virtual environment (keyring is Windows-only here).
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet "paho-mqtt>=2.0,<3" "psutil>=5.9,<8"

CONFIG="$APP_DIR/config.ini"
if [[ -f "$CONFIG" ]]; then
    echo "Keeping the existing config.ini ($CONFIG)."
else
    default_id="$(hostname | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_]/_/g')"
    read -rp "MQTT broker (IP or host name): " host
    read -rp "MQTT port [1883]: " port; port="${port:-1883}"
    read -rp "MQTT user name: " user
    read -rsp "MQTT password: " password; echo
    read -rp "Device name in Home Assistant [$(hostname)]: " name; name="${name:-$(hostname)}"
    echo "Replacing an existing PC in Home Assistant? Enter its device ID (the part"
    echo "before _cpu in the unique ID of its CPU sensor) to keep all entity IDs."
    read -rp "Device ID [$default_id]: " dev_id; dev_id="${dev_id:-$default_id}"
    read -rp "Language, en or nl [en]: " lang; lang="${lang:-en}"

    umask 077
    cat > "$CONFIG" <<EOF
[mqtt]
host = $host
port = $port
username = $user
; Only readable by you (chmod 600).
password = $password

[device]
name = $name
id = $dev_id
language = $lang

[agent]
interval = 15
in_use_idle_minutes = 5
allow_power_commands = true
; Seconds to wait on shut down/restart
shutdown_delay = 5
EOF
    chmod 600 "$CONFIG"
fi

cat > "$UNIT_DIR/$UNIT" <<EOF
[Unit]
Description=Desktop MQTT Agent (PC status for Home Assistant)
After=network-online.target

[Service]
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/desktop_mqtt_agent.py --config $CONFIG
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$UNIT" >/dev/null
systemctl --user restart "$UNIT"
sleep 3
if systemctl --user is-active --quiet "$UNIT"; then
    echo "Agent is running. Log: $APP_DIR/desktop_mqtt_agent.log"
else
    echo "Agent did not start. Check: journalctl --user -u $UNIT -n 50"
fi

# Wake-on-LAN: NetworkManager otherwise leaves the network card's setting to chance.
if command -v nmcli >/dev/null; then
    conn="$(nmcli -t -f NAME,TYPE connection show --active | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
    if [[ -n "$conn" ]]; then
        current="$(nmcli -g 802-3-ethernet.wake-on-lan connection show "$conn")"
        if [[ "$current" != *magic* ]]; then
            read -rp "Turn on Wake-on-LAN for wired connection '$conn'? [Y/n] " wol
            if [[ "${wol:-Y}" =~ ^[Yy]$ ]]; then
                nmcli connection modify "$conn" 802-3-ethernet.wake-on-lan magic \
                    && echo "Wake-on-LAN turned on (takes effect after the next reconnect or reboot)."
            fi
        fi
    fi
fi
