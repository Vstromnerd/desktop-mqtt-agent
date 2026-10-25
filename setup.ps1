# Desktop MQTT Agent - all-in-one installer. Start it through INSTALL.bat.
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
Set-Location $dir

$taskName   = "Desktop MQTT Agent"
$legacyTask = "HA PC Agent"          # task name used before the rename
$script     = "desktop_mqtt_agent.py"
$logName    = "desktop_mqtt_agent.log"

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }

try {
    # 0. Check that the script itself is here
    if (-not (Test-Path (Join-Path $dir $script))) {
        Write-Host "Files in this folder:"; Get-ChildItem $dir -Name | ForEach-Object { Write-Host "  $_" }
        throw "$script is not in this folder."
    }

    # 1. Find Python
    Step "Looking for Python"
    $python = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                            "C:\Program Files\Python3*\python.exe" -ErrorAction SilentlyContinue |
              Select-Object -First 1 -ExpandProperty FullName
    if (-not $python) { throw "Python not found. Install Python 3 from python.org and run this again." }
    $pythonw = Join-Path (Split-Path $python) "pythonw.exe"
    Write-Host "Found: $python"

    # 2. Required packages
    Step "Installing required packages (this can take a moment)"
    & $python -m pip install --disable-pip-version-check --quiet -r (Join-Path $dir "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Installing the packages failed." }
    Write-Host "Done."

    # 3. Settings
    $configPath = Join-Path $dir "config.ini"
    if (Test-Path $configPath) {
        Step "config.ini already exists, using it"
    } else {
        Step "Home Assistant MQTT broker"
        $mqttHost = ""
        while (-not $mqttHost) { $mqttHost = Read-Host "IP address or host name of the broker (e.g. 192.168.1.10)" }
        $port = Read-Host "Port [1883]"
        if (-not $port) { $port = "1883" }

        Step "Name of this PC in Home Assistant"
        $default = "PC $(($env:USERNAME -split '\.')[0])"
        $name = Read-Host "Name [$default]"
        if (-not $name) { $name = $default }
        $lang = Read-Host "Language of the entity names, en or nl [en]"
        if ($lang -ne "nl") { $lang = "en" }

        Step "MQTT login"
        $user = Read-Host "User name [pc-agent]"
        if (-not $user) { $user = "pc-agent" }
        $secure = Read-Host "Password" -AsSecureString
        $pass = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))

        # Store the password in Windows Credential Manager (through stdin, so it never shows in the process list)
        $OutputEncoding = New-Object System.Text.UTF8Encoding $false   # keeps characters like e-umlaut intact
        $pass | & $python -c "import keyring, sys; keyring.set_password('desktop_mqtt_agent', sys.argv[1], sys.stdin.buffer.readline().decode('utf-8').rstrip('\r\n'))" $user
        if ($LASTEXITCODE -ne 0) { throw "Saving the password in Windows Credential Manager failed." }
        $pass = $null
        Write-Host "Password saved in Windows Credential Manager."

        $config = @(
            "[mqtt]",
            "host = $mqttHost",
            "port = $port",
            "username = $user",
            "; the password is stored in Windows Credential Manager (desktop_mqtt_agent)",
            "password =",
            "",
            "[device]",
            "name = $name",
            "language = $lang",
            "",
            "[agent]",
            "interval = 15",
            "in_use_idle_minutes = 5",
            "allow_power_commands = true",
            "shutdown_delay = 5",
            "",
            "[lhm]",
            "enabled = false"
        )
        Set-Content -Path $configPath -Value $config -Encoding UTF8
        Write-Host "config.ini created."
    }

    # 4. Check the connection (host and port are read from config.ini)
    Step "Checking the connection to the broker"
    $ini = Get-Content $configPath
    $mqttHost = (($ini | Where-Object { $_ -match '^\s*host\s*=' } | Select-Object -First 1) -split '=', 2)[1].Trim()
    $portLine = $ini | Where-Object { $_ -match '^\s*port\s*=' } | Select-Object -First 1
    $port = if ($portLine) { [int](($portLine -split '=', 2)[1].Trim()) } else { 1883 }
    $ok = Test-NetConnection $mqttHost -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue
    if (-not $ok) { throw "Cannot reach the broker at $mqttHost, port $port." }
    Write-Host "Reachable."

    # 5. Start automatically at logon
    Step "Setting the agent to start at logon"
    # Stop running copies first (also the task from before the rename), otherwise two agents run side by side.
    foreach ($t in @($taskName, $legacyTask)) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
            if ($t -eq $legacyTask) {
                Unregister-ScheduledTask -TaskName $t -Confirm:$false
                Write-Host "Old task '$legacyTask' removed."
            }
        }
    }
    Start-Sleep -Seconds 2
    Remove-Item (Join-Path $dir $logName) -ErrorAction SilentlyContinue
    $action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$dir\$script`"" -WorkingDirectory $dir
    $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
                  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
                  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "Publishes PC status to Home Assistant over MQTT" -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName

    # 6. Check that it connects
    Step "Checking that the agent connects"
    $log = Join-Path $dir $logName
    $connected = $false
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        if ((Test-Path $log) -and (Select-String -Path $log -Pattern "Connected to MQTT broker" -Quiet)) {
            $connected = $true; break
        }
    }
    if ($connected) {
        Write-Host ""
        Write-Host "DONE! Your PC is now in Home Assistant under Settings > Devices & services > MQTT." -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host "The agent is running but has not connected (yet)." -ForegroundColor Yellow
        Write-Host "Last lines of the log file:"
        if (Test-Path $log) { Get-Content $log -Tail 5 }
        Write-Host "Wrong password? Delete config.ini and run INSTALL.bat again."
    }
}
catch {
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
}
