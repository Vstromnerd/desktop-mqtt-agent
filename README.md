# Desktop MQTT Agent

A small Python agent that publishes the status of a Windows PC to [Home Assistant](https://www.home-assistant.io/) over MQTT. Through MQTT discovery, everything shows up automatically as one device, with no custom integration needed.

It is meant to be light and easy to read: one Python file, one config file, one installer.

## What you get

| Entity | What it shows |
| --- | --- |
| CPU usage, Memory usage, Disk C: usage | percentages |
| Idle | seconds since the last mouse or keyboard input |
| Last boot | time of the last start-up |
| Active app | process name of the foreground window (e.g. `chrome.exe`), never the window title |
| Locked | on while the lock screen is shown |
| In use | on when the PC was used in the last 5 minutes and is not locked |
| Lock, Sleep, Shut down, Restart | buttons |
| CPU/GPU temperature, GPU usage | optional, through LibreHardwareMonitor |

When the PC is off, asleep, or nobody is logged on, all entities become **unavailable**. With the sleep and shut down buttons this happens right away; otherwise within about 45 seconds.

Entity names are available in English and Dutch (`language = en` or `nl` in `config.ini`).

## Requirements

- Windows 10 or 11
- [Python 3](https://www.python.org/downloads/windows/) (tick **Add python.exe to PATH** during installation)
- An MQTT broker that Home Assistant uses, for example the **Mosquitto broker** app in Home Assistant, with the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) set up

## Installation

1. **Create an MQTT user.** With the Mosquitto broker app, any Home Assistant user can log in to the broker. Go to **Settings → People → Users → Add user**, for example `pc-agent`, with *Can only log in from the local network* on and no administrator rights.
2. **Put the files in a fixed place**, for example `C:\Tools\desktop-mqtt-agent`. The agent runs from that folder.
3. **Run `INSTALL.bat`.** It:
   - installs the Python packages from `requirements.txt`;
   - asks for the broker address, a name for the PC, the language and the MQTT login;
   - stores the password in **Windows Credential Manager**, not in a file;
   - checks that the broker is reachable;
   - adds a scheduled task so the agent starts at every logon, and starts it right away.

The PC then shows up under **Settings → Devices & services → MQTT**.

To remove it: `Unregister-ScheduledTask -TaskName "Desktop MQTT Agent"` in PowerShell, then delete the folder.

### Testing by hand

```powershell
python desktop_mqtt_agent.py --dry-run
```

This prints the discovery message and one reading without connecting. To run it in the foreground (stop with Ctrl+C):

```powershell
python desktop_mqtt_agent.py
```

## Temperatures (optional)

Windows does not report temperatures itself. For that you need LibreHardwareMonitor.

1. Install and start [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor).
2. Turn on **Options → Remote Web Server → Run** (port 8085).
3. In `config.ini`, set `enabled = true` under `[lhm]` and restart the agent.

The default sensor paths match an AMD Ryzen CPU (`Core (Tctl/Tdie)`). If LibreHardwareMonitor shows a different name, change the path in `config.ini`.

## Security

- **Anyone who can publish to your MQTT broker can lock, sleep, shut down or restart the PC** through the command topic. Use a broker with authentication, and set `allow_power_commands = false` if you don't want the power buttons. Locking always stays available.
- The MQTT password is kept in Windows Credential Manager. If you type it in `config.ini` by hand, the agent moves it there on first start and clears the line.
- The agent sends only the process name of the active app, never window titles.

## Good to know

- **Sleep:** with hibernation enabled, the sleep button makes Windows hibernate instead. Turn hibernation off with `powercfg /hibernate off` (as administrator).
- **Problems:** see `desktop_mqtt_agent.log` in the same folder.
- **Upgrading from "HA PC Agent":** run `INSTALL.bat` again. It removes the old scheduled task, and the agent takes over the stored password. Entity IDs and the Home Assistant device stay the same. Add `language = nl` to `config.ini` to keep the Dutch entity names.

## MQTT topics

| Topic | Contents |
| --- | --- |
| `homeassistant/device/<id>/config` | discovery (retained) |
| `desktop_mqtt_agent/<id>/state` | JSON with all readings |
| `desktop_mqtt_agent/<id>/availability` | `online` / `offline` (retained, also the last will) |
| `desktop_mqtt_agent/<id>/command` | `lock`, `sleep`, `shutdown`, `restart` |

`<id>` is the computer name in lower case, or `id` from `config.ini`.

## License

[MIT](LICENSE)

This is a community project and is not affiliated with or endorsed by Home Assistant or the Open Home Foundation.
