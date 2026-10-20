<#
.SYNOPSIS
  One command to set up and start the fully self-hosted ATLAS demo (browser test call).

.DESCRIPTION
  Safe to re-run. Checks the machine, creates .env with generated secrets if it is missing,
  installs the code dependencies, starts the Docker services (database, voice service and
  the self-hosted models), migrates and seeds the database, downloads missing models,
  starts the API, worker and web app in the background, configures the self-hosted model
  connections and speed preset through the API, publishes the demo agent and warms the
  models. Nothing is duplicated or overwritten on a second run.

  Default preset: private_pocket_tts (Whisper + qwen2.5:1.5b + Pocket TTS, works with no
  internet). With -DeepgramKey (or env ATLAS_DEMO_DEEPGRAM_KEY) and internet at setup time it
  also saves a live-transcription connection to the local streaming server and uses
  private_streaming. The voice service checks that key online once, when the connection is
  saved; calls afterwards send audio only to the local server.

  See docs/university-demo.md.

.PARAMETER Stop
  Stop what this script started (API, worker, web) and the demo's Docker containers.
.PARAMETER Restart
  Also stop programs that this script did not start but that hold the ATLAS ports
  (8080 API, 5173 web), then start fresh ones.
.PARAMETER DryRun
  Show what would be done without changing anything. Alias: -WhatIf.
.PARAMETER Preset
  auto (default): streaming when its connection exists or can be saved, else Pocket TTS.
  pocket: always private_pocket_tts. streaming: try private_streaming first.
.PARAMETER DeepgramKey
  A real Deepgram key, only needed to save the live-transcription connection (needs internet).
  Never printed. Prefer env ATLAS_DEMO_DEEPGRAM_KEY so it does not end up in shell history.
.PARAMETER HfToken
  Optional Hugging Face token that turns on Pocket TTS voice cloning (AI providers > Voice >
  Clone a voice). The account must have accepted the terms of the gated kyutai/pocket-tts
  weights first (docs/local-models.md, "Voice cloning"). Saved in .env as
  POCKET_TTS_HF_TOKEN and passed to the local-tts container as HF_TOKEN; never printed.
  Prefer env ATLAS_DEMO_HF_TOKEN so it does not end up in shell history.
.PARAMETER SkipWarm
  Do not load the models into memory at the end.
.PARAMETER FastStt
  Use Whisper tiny.en instead of base: about 0.7 s faster per reply on a 6-core CPU, English
  only, and less robust to accents and noise. Try it, and run without it to switch back.
.PARAMETER Rebuild
  Rebuild the two locally built model images (needs internet).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File infra\scripts\setup_local_demo.ps1
.EXAMPLE
  .\make.ps1 demo ; .\make.ps1 demo -DryRun ; .\make.ps1 demo-stop
#>
param(
  [switch]$Stop,
  [switch]$Restart,
  [Alias("WhatIf")][switch]$DryRun,
  [ValidateSet("auto", "pocket", "streaming")][string]$Preset = "auto",
  [string]$DeepgramKey = "",
  [string]$HfToken = "",
  [switch]$SkipWarm,
  [switch]$FastStt,
  [switch]$Rebuild
)

# Native tools write progress to stderr; with "Stop", Windows PowerShell would turn that into
# terminating errors. Every native call below checks $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ApiDir = Join-Path $Root "services\api"
$WebDir = Join-Path $Root "apps\web"
$RunDir = Join-Path $Root ".atlas-run"
$DepsDir = Join-Path $RunDir "deps"
$LogDir = Join-Path $env:TEMP "atlas-demo"
$EnvFile = Join-Path $Root ".env"
$EnvExample = Join-Path $Root ".env.example"
$Profiles = @("--profile", "voice", "--profile", "local-ai")
$ApiUrl = "http://127.0.0.1:8080"
$WebUrl = "http://localhost:5173"
$LlmModel = "qwen2.5:1.5b"
$SttModel = "Systran/faster-whisper-base"
if ($FastStt) { $SttModel = "Systran/faster-whisper-tiny.en" }
$CoreServices = @("postgres", "redis", "voice-db", "voice-redis", "voice-storage", "voice-runtime",
  "local-llm", "local-speech", "local-tts")
# Docker Desktop's own processes publish container ports on the host.
$DockerPortOwners = @("com.docker.backend", "com.docker.proxy", "wslrelay", "vpnkit", "docker-proxy", "dockerd")

$script:Warnings = New-Object System.Collections.Generic.List[string]
$script:GeneratedPassword = $null

# ------------------------------------------------------------------------------ output
function Step([string]$m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Info([string]$m) { Write-Host "    $m" }
function Warn([string]$m) { Write-Host "    WARNING: $m" -ForegroundColor Yellow; $script:Warnings.Add($m) }
function Fail([string]$m) {
  Write-Host ""
  Write-Host "ERROR: $m" -ForegroundColor Red
  Cleanup-Env
  Pop-Location -ErrorAction SilentlyContinue
  exit 1
}
function Plan([string]$m) { Write-Host "    [dry run] would $m" -ForegroundColor DarkGray }

# The two secrets are only put in this process's environment for the child that needs them;
# whatever the caller's session had is restored at the end.
$script:OrigEnv = @{
  "ATLAS_DEMO_PASSWORD"     = $env:ATLAS_DEMO_PASSWORD
  "ATLAS_DEMO_DEEPGRAM_KEY" = $env:ATLAS_DEMO_DEEPGRAM_KEY
  "ATLAS_DEMO_HF_TOKEN"     = $env:ATLAS_DEMO_HF_TOKEN
}
function Cleanup-Env {
  foreach ($k in $script:OrigEnv.Keys) {
    if ($script:OrigEnv[$k]) { Set-Item -Path "Env:$k" -Value $script:OrigEnv[$k] }
    else { Remove-Item "Env:$k" -ErrorAction SilentlyContinue }
  }
}

# ------------------------------------------------------------------------------ helpers
function Test-Command([string]$name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Get-Major([string]$version) {
  $m = [regex]::Match($version, "(\d+)\.")
  if ($m.Success) { return [int]$m.Groups[1].Value }
  return 0
}

function Get-HttpStatus([string]$url, [int]$timeout = 5) {
  try {
    $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $timeout -ErrorAction Stop
    return [int]$r.StatusCode
  } catch {
    if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
    return 0
  }
}

function Get-Json([string]$url, [int]$timeout = 5) {
  try { return Invoke-RestMethod -Uri $url -TimeoutSec $timeout -ErrorAction Stop } catch { return $null }
}

function New-Secret([int]$bytes = 48) {
  $b = New-Object byte[] $bytes
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($b)
  $rng.Dispose()
  return [Convert]::ToBase64String($b).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function New-FernetKey {
  # Fernet: url-safe base64 of 32 random bytes, padding kept.
  $b = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($b)
  $rng.Dispose()
  return [Convert]::ToBase64String($b).Replace("+", "-").Replace("/", "_")
}

function Read-EnvFile([string]$path) {
  $map = @{}
  if (-not (Test-Path $path)) { return $map }
  foreach ($line in [System.IO.File]::ReadAllLines($path)) {
    $m = [regex]::Match($line, "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
    if ($m.Success) { $map[$m.Groups[1].Value] = $m.Groups[2].Value.Trim().Trim('"').Trim("'") }
  }
  return $map
}

function Write-EnvValues([string]$path, [hashtable]$values) {
  # Replace KEY=... lines in place; append keys that are not there. UTF-8 without BOM, LF.
  $text = [System.IO.File]::ReadAllText($path)
  $lines = New-Object System.Collections.Generic.List[string]
  foreach ($l in ($text -split "`n")) { $lines.Add($l.TrimEnd("`r")) }
  if ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq "") { $lines.RemoveAt($lines.Count - 1) }
  $done = @{}
  for ($i = 0; $i -lt $lines.Count; $i++) {
    $m = [regex]::Match($lines[$i], "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    if ($m.Success -and $values.ContainsKey($m.Groups[1].Value)) {
      $k = $m.Groups[1].Value
      $lines[$i] = "$k=" + $values[$k]
      $done[$k] = $true
    }
  }
  $added = $false
  foreach ($k in $values.Keys) {
    if (-not $done.ContainsKey($k)) {
      if (-not $added) { $lines.Add(""); $lines.Add("# Added by infra/scripts/setup_local_demo"); $added = $true }
      $lines.Add("$k=" + $values[$k])
    }
  }
  $utf8 = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($path, (($lines -join "`n") + "`n"), $utf8)
}

function Test-VolumeExists([string]$name) {
  docker volume inspect $name 1>$null 2>$null
  return ($LASTEXITCODE -eq 0)
}

function Get-PortOwner([int]$port) {
  $c = $null
  try { $c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop | Select-Object -First 1 } catch { $c = $null }
  if ($c) { return [int]$c.OwningProcess }
  foreach ($line in (netstat -ano -p TCP 2>$null)) {
    $m = [regex]::Match($line, "^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)")
    if ($m.Success -and [int]$m.Groups[1].Value -eq $port) { return [int]$m.Groups[2].Value }
  }
  # IPv6 lines look like [::1]:5173
  foreach ($line in (netstat -ano 2>$null)) {
    $m = [regex]::Match($line, "^\s*TCP\s+\[[^\]]*\]:(\d+)\s+\S+\s+LISTENING\s+(\d+)")
    if ($m.Success -and [int]$m.Groups[1].Value -eq $port) { return [int]$m.Groups[2].Value }
  }
  return 0
}

function Get-ProcessName([int]$procId) {
  $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
  if ($p) { return $p.ProcessName }
  return "unknown"
}

function Test-IsDescendant([int]$child, [int]$ancestor) {
  $cur = $child
  for ($i = 0; $i -lt 8 -and $cur -gt 0; $i++) {
    if ($cur -eq $ancestor) { return $true }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    $cur = [int]$p.ParentProcessId
  }
  return $false
}

function Get-PidFile([string]$name) { return (Join-Path $RunDir "$name.pid") }

function Get-OwnProcess([string]$name) {
  # The pid this script recorded, if that process is still the one it started.
  $file = Get-PidFile $name
  if (-not (Test-Path $file)) { return 0 }
  $parts = (Get-Content $file -ErrorAction SilentlyContinue) -split "\s+"
  if ($parts.Count -lt 2) { return 0 }
  $procId = [int]$parts[0]
  $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
  if (-not $p) { return 0 }
  try { if ($p.StartTime.ToUniversalTime().Ticks.ToString() -ne $parts[1]) { return 0 } } catch { return 0 }
  return $procId
}

function Stop-Tree([int]$procId) {
  # The venv launchers start the real interpreter as a child: stop the whole tree.
  taskkill /PID $procId /T /F 1>$null 2>$null
}

function Stop-Own([string]$name) {
  $procId = Get-OwnProcess $name
  $stopped = $false
  if ($procId -gt 0) {
    if ($DryRun) { Plan "stop $name (pid $procId, started by this script)" ; return $false }
    Stop-Tree $procId
    Info "stopped $name (pid $procId, started by this script earlier)"
    $stopped = $true
  }
  if (-not $DryRun) { Remove-Item (Get-PidFile $name) -ErrorAction SilentlyContinue }
  return $stopped
}

function Wait-PortFree([int]$port, [int]$seconds = 20) {
  for ($i = 0; $i -lt $seconds; $i++) {
    if ((Get-PortOwner $port) -eq 0) { return $true }
    Start-Sleep -Seconds 1
  }
  return $false
}

function Start-Background([string]$name, [string]$file, [string[]]$arguments, [string]$dir) {
  $out = Join-Path $LogDir "$name.log"
  $err = Join-Path $LogDir "$name.err.log"
  $p = Start-Process -FilePath $file -ArgumentList $arguments -WorkingDirectory $dir -WindowStyle Hidden `
    -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
  if (-not $p) { Fail "could not start $name" }
  $ticks = $p.StartTime.ToUniversalTime().Ticks
  Set-Content -Path (Get-PidFile $name) -Value "$($p.Id) $ticks" -Encoding ascii
  Info "started $name (pid $($p.Id)); log: $out"
  return $p.Id
}

function Show-LogTail([string]$name) {
  foreach ($f in @((Join-Path $LogDir "$name.err.log"), (Join-Path $LogDir "$name.log"))) {
    if (Test-Path $f) {
      Write-Host "    --- last lines of $f" -ForegroundColor DarkGray
      Get-Content $f -Tail 15 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    }
  }
}

function Invoke-Compose([string[]]$composeArgs) {
  # Out-Host: docker's output must not become this function's return value.
  & docker compose @Profiles @composeArgs | Out-Host
  return ($LASTEXITCODE -eq 0)
}

function Get-ContainerState([string]$service) {
  $id = (docker compose @Profiles ps -a -q $service 2>$null | Select-Object -First 1)
  if (-not $id) { return $null }
  $fmt = "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.State.OOMKilled}}|{{.State.ExitCode}}"
  $raw = docker inspect --format $fmt $id 2>$null
  if (-not $raw) { return $null }
  $p = $raw.Split("|")
  return @{ Status = $p[0]; Health = $p[1]; OOM = ($p[2] -eq "true"); Exit = $p[3] }
}

function Wait-Service([string]$service, [int]$seconds, [string]$probeUrl = "") {
  $deadline = (Get-Date).AddSeconds($seconds)
  $lastNote = Get-Date
  while ((Get-Date) -lt $deadline) {
    $s = Get-ContainerState $service
    if ($s) {
      if ($s.OOM -or $s.Exit -eq "137") {
        Warn "$service was stopped for lack of memory (out-of-memory kill). Close other programs or give Docker more memory, then re-run."
        return $false
      }
      if ($s.Status -eq "exited" -or $s.Status -eq "dead") {
        Warn "$service exited (code $($s.Exit)). See: docker compose $($Profiles -join ' ') logs --tail 50 $service"
        return $false
      }
      $ready = $false
      if ($s.Health -ne "none") { $ready = ($s.Health -eq "healthy") }
      elseif ($s.Status -eq "running") { $ready = ($probeUrl -eq "") -or ((Get-HttpStatus $probeUrl 3) -eq 200) }
      if ($ready) { Info "$service is ready"; return $true }
    }
    if (((Get-Date) - $lastNote).TotalSeconds -ge 30) {
      Info "still waiting for $service ..."
      $lastNote = Get-Date
    }
    Start-Sleep -Seconds 3
  }
  Warn "$service did not become ready within $seconds s. See: docker compose $($Profiles -join ' ') logs --tail 50 $service"
  return $false
}

function Invoke-ApiPython([string[]]$pyArgs) {
  # Runs a Python script with the API's environment; streams output and returns it.
  Push-Location $ApiDir
  try {
    $lines = New-Object System.Collections.Generic.List[string]
    & uv run --no-sync python @pyArgs | ForEach-Object { Write-Host $_; $lines.Add([string]$_) }
    $script:LastPyExit = $LASTEXITCODE
    return $lines
  } finally { Pop-Location }
}

# ------------------------------------------------------------------------------ stop
if ($Stop) {
  Step "Stopping the ATLAS demo"
  foreach ($n in @("web", "worker", "api")) { $null = Stop-Own $n }
  if (-not $DryRun) { $null = Wait-PortFree 8080 10; $null = Wait-PortFree 5173 10 }
  foreach ($port in @(8080, 5173)) {
    $owner = Get-PortOwner $port
    if ($owner -gt 0 -and -not $DryRun) {
      Info "port $port is still used by $(Get-ProcessName $owner) (pid $owner), which this script did not start: left running"
    }
  }
  Push-Location $Root
  if ($DryRun) { Plan "docker compose $($Profiles -join ' ') stop" }
  else {
    if (Invoke-Compose @("stop")) { Info "Docker containers stopped (data and models are kept)" }
    else { Warn "docker compose stop failed (is Docker running?)" }
  }
  Pop-Location
  exit 0
}

# ------------------------------------------------------------------------------ main
$key = $DeepgramKey
if (-not $key -and $env:ATLAS_DEMO_DEEPGRAM_KEY) { $key = $env:ATLAS_DEMO_DEEPGRAM_KEY }
$key = $key.Trim()
$hfToken = $HfToken
if (-not $hfToken -and $env:ATLAS_DEMO_HF_TOKEN) { $hfToken = $env:ATLAS_DEMO_HF_TOKEN }
$hfToken = $hfToken.Trim()
# Not inherited by the API, worker and web app started below (restored at the end).
Remove-Item Env:ATLAS_DEMO_DEEPGRAM_KEY -ErrorAction SilentlyContinue
Remove-Item Env:ATLAS_DEMO_HF_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:ATLAS_DEMO_PASSWORD -ErrorAction SilentlyContinue

if ($DryRun) { Write-Host "Dry run: nothing will be changed." -ForegroundColor DarkGray }
Push-Location $Root

# 1. preflight -------------------------------------------------------------------
Step "Checking this machine"
if (-not (Test-Command "docker")) { Fail "Docker is not installed. Install Docker Desktop (https://www.docker.com/products/docker-desktop/) and re-run." }
docker info --format "{{.ServerVersion}}" 1>$null 2>$null
if ($LASTEXITCODE -ne 0) { Fail "Docker is not running. Start Docker Desktop, wait until it shows 'Engine running', then re-run." }
$composeVersion = docker compose version --short 2>$null
if ($LASTEXITCODE -ne 0) { Fail "Docker Compose v2 is missing ('docker compose version' failed). Update Docker Desktop." }
Info "Docker Compose $composeVersion"

if (-not (Test-Command "node")) { Fail "Node.js is not installed. Install Node.js 22 LTS (https://nodejs.org/) and re-run." }
$nodeVersion = (node --version 2>$null)
$nodeMajor = Get-Major ($nodeVersion -replace "^v", "")
if ($nodeMajor -lt 22) { Fail "Node.js $nodeVersion is too old. Install Node.js 22 LTS." }
if ($nodeMajor -ne 22) { Warn "Node.js ${nodeVersion}: ATLAS is tested with Node.js 22 LTS (newer versions usually work)." }
else { Info "Node.js $nodeVersion" }

if (-not (Test-Command "pnpm")) { Fail "pnpm is not installed. Run: npm install -g pnpm@10   (then open a new terminal)" }
$pnpmVersion = (pnpm --version 2>$null)
$pnpmMajor = Get-Major $pnpmVersion
if ($pnpmMajor -lt 10) { Fail "pnpm $pnpmVersion is too old. Run: npm install -g pnpm@10" }
if ($pnpmMajor -ne 10) { Warn "pnpm ${pnpmVersion}: the web app pins pnpm 10. A newer pnpm downloads pnpm 10 on first use, which fails without internet: install pnpm 10 on the demo PC (npm install -g pnpm@10)." }
else { Info "pnpm $pnpmVersion" }

if (-not (Test-Command "uv")) { Fail "uv is not installed. Run: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"  (then open a new terminal)" }
Info ((uv --version 2>$null) -join " ")

$os = Get-CimInstance Win32_OperatingSystem
$ramTotal = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$ramFree = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
$cores = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
Info "RAM $ramTotal GB total, $ramFree GB free; $cores logical CPU cores"
# A "16 GB" machine shows about 15.8 GB; warn below 15 GB.
if ($os.TotalVisibleMemorySize -lt 15000000) { Warn "This machine has $ramTotal GB of RAM; 16 GB or more is recommended. Replies will be slower; close other programs before the demo." }
if ($ramFree -lt 6) { Warn "Only $ramFree GB of RAM is free; the models need about 6 GB. Close browser tabs and other programs." }
if ($cores -lt 8) { Warn "$cores CPU cores; 8 or more are recommended for snappy replies on CPU." }
$dockerMem = docker info --format "{{.MemTotal}}" 2>$null
if ($dockerMem) {
  $dockerGb = [math]::Round([double]$dockerMem / 1GB, 1)
  Info "Docker can use $dockerGb GB of memory"
  if ($dockerGb -lt 7) { Warn "Docker can use only $dockerGb GB of memory; the demo needs about 7 GB. Raise it in Docker Desktop (Settings > Resources) or in %UserProfile%\.wslconfig ([wsl2] memory=10GB), then restart Docker." }
}
$drive = New-Object System.IO.DriveInfo((Split-Path -Qualifier $Root))
$freeGb = [math]::Round($drive.AvailableFreeSpace / 1GB, 1)
Info "Free disk on $($drive.Name) $freeGb GB"
if ($freeGb -lt 25) { Warn "Only $freeGb GB free on $($drive.Name). The first setup needs about 25 GB (Docker images and models; Docker's own disk may be on another drive)." }

foreach ($port in @(5433, 6380, 8000, 9000, 11434, 8001, 8002, 8003)) {
  $owner = Get-PortOwner $port
  if ($owner -gt 0) {
    $pname = Get-ProcessName $owner
    if ($DockerPortOwners -notcontains $pname) {
      Warn "Port $port is used by $pname (pid $owner). A demo container needs it; close that program."
    }
  }
}

# uv.toml points the cache at a D: folder on the development machine; use the default cache
# when that drive does not exist here.
if (-not $env:UV_CACHE_DIR) {
  $uvToml = Join-Path $ApiDir "uv.toml"
  if (Test-Path $uvToml) {
    $m = [regex]::Match((Get-Content $uvToml -Raw), 'cache-dir\s*=\s*"([^"]+)"')
    if ($m.Success) {
      $qual = Split-Path -Qualifier $m.Groups[1].Value -ErrorAction SilentlyContinue
      if ($qual -and -not (Test-Path "$qual\")) {
        $env:UV_CACHE_DIR = Join-Path $env:LOCALAPPDATA "uv\cache"
        Info "uv cache: $($env:UV_CACHE_DIR) (the drive in services/api/uv.toml does not exist here)"
      }
    }
  }
}
$useDeps = Test-Path (Join-Path $DepsDir "uv-cache")
if ($useDeps) {
  $env:UV_CACHE_DIR = Join-Path $DepsDir "uv-cache"
  if (Test-Path (Join-Path $DepsDir "uv-python")) { $env:UV_PYTHON_INSTALL_DIR = Join-Path $DepsDir "uv-python" }
  Info "using the code dependencies from the offline bundle in $DepsDir"
}

# 2. .env -------------------------------------------------------------------------
Step "Settings file (.env)"
$secretKeys = [ordered]@{
  "ATLAS_JWT_SECRET"                   = "secret"
  "ATLAS_SECRETS_ENCRYPTION_KEY"       = "fernet"
  "ATLAS_VOICE_RUNTIME_JWT_SECRET"     = "secret"
  "ATLAS_VOICE_RUNTIME_METRICS_SECRET" = "secret"
  "ATLAS_VOICE_STORAGE_PASSWORD"       = "secret"
}
$placeholders = @("", "replace-with-a-long-random-secret", "change-me")
$envIsNew = -not (Test-Path $EnvFile)
if ($envIsNew) {
  if (-not (Test-Path $EnvExample)) { Fail ".env.example is missing; is this the ATLAS repository?" }
  if ($DryRun) { Plan "create .env from .env.example" }
  else { Copy-Item $EnvExample $EnvFile; Info "created .env from .env.example" }
} else { Info ".env exists: keeping every value that is already set" }

$envMap = if ($envIsNew) { Read-EnvFile $EnvExample } else { Read-EnvFile $EnvFile }
$updates = @{}
foreach ($k in $secretKeys.Keys) {
  $cur = if ($envMap.ContainsKey($k)) { $envMap[$k] } else { "" }
  if ($placeholders -notcontains $cur) { continue }
  # An existing storage volume was initialised with the compose default password.
  if ($k -eq "ATLAS_VOICE_STORAGE_PASSWORD" -and (Test-VolumeExists "atlas_voice_storage_data")) { continue }
  if ($secretKeys[$k] -eq "fernet") { $updates[$k] = New-FernetKey } else { $updates[$k] = New-Secret 48 }
}
$demoPassword = if ($envMap.ContainsKey("ATLAS_DEMO_PASSWORD")) { $envMap["ATLAS_DEMO_PASSWORD"] } else { "" }
if (-not $demoPassword) {
  $demoPassword = New-Secret 15
  $updates["ATLAS_DEMO_PASSWORD"] = $demoPassword
  $script:GeneratedPassword = $demoPassword
}
if ($updates.Count -gt 0) {
  $names = ($updates.Keys | Sort-Object) -join ", "
  if ($DryRun) { Plan "generate (random, not shown): $names" }
  else { Write-EnvValues $EnvFile $updates; Info "generated (random, not shown): $names" }
} else { Info "all required secrets are set" }

# Pocket TTS voice cloning: where the host-run API reaches the server, and the optional token.
$ttsUpdates = @{}
if (-not $envMap.ContainsKey("ATLAS_LOCAL_TTS_URL")) { $ttsUpdates["ATLAS_LOCAL_TTS_URL"] = "http://127.0.0.1:8002/v1" }
if ($hfToken) {
  if ($hfToken -notmatch "^[A-Za-z0-9_-]{8,200}$") {
    Warn "The Hugging Face token has unexpected characters; it was not saved. Voice cloning stays off."
  } else { $ttsUpdates["POCKET_TTS_HF_TOKEN"] = $hfToken }
}
if ($ttsUpdates.Count -gt 0) {
  $names = ($ttsUpdates.Keys | Sort-Object) -join ", "
  if ($DryRun) { Plan "set in .env (token values not shown): $names" }
  else { Write-EnvValues $EnvFile $ttsUpdates; Info "set in .env (token values not shown): $names" }
}
$cloningTokenSet = [bool]$ttsUpdates["POCKET_TTS_HF_TOKEN"] -or [bool]($envMap["POCKET_TTS_HF_TOKEN"])

# 3. code dependencies -------------------------------------------------------------
Step "Code dependencies (API and web app)"
if ($DryRun) {
  Plan "run 'uv sync --frozen' in services/api (offline from the cache if there is no internet)"
  Plan "run 'pnpm install --frozen-lockfile' in apps/web (offline from the store if there is no internet)"
} else {
  Push-Location $ApiDir
  if ($useDeps) { uv sync --frozen --offline } else { uv sync --frozen }
  if ($LASTEXITCODE -ne 0 -and -not $useDeps) { Info "retrying offline from the uv cache"; uv sync --frozen --offline }
  if ($LASTEXITCODE -ne 0) {
    if (Test-Path (Join-Path $ApiDir ".venv\Scripts\python.exe")) { Warn "uv sync failed; using the existing services/api/.venv as it is." }
    else { Pop-Location; Fail "Installing the API's Python packages failed. Connect to the internet once, or import an offline bundle made with --with-deps." }
  }
  Pop-Location

  Push-Location $WebDir
  $storeArgs = @()
  $bundleStore = Join-Path $DepsDir "pnpm-store"
  if (Test-Path $bundleStore) { $storeArgs = @("--store-dir", $bundleStore) }
  else {
    # Keep the store node_modules was linked from: with another store pnpm wants to delete
    # and rebuild node_modules (and refuses without a terminal).
    $modulesYaml = Join-Path $WebDir "node_modules\.modules.yaml"
    if (Test-Path $modulesYaml) {
      $sm = Select-String -Path $modulesYaml -Pattern "^storeDir:\s*(.+)$" | Select-Object -First 1
      if ($sm) {
        $storeDir = ($sm.Matches[0].Groups[1].Value.Trim().Trim("'").Trim('"')) -replace "[\/]v\d+$", ""
        if (Test-Path $storeDir) { $storeArgs = @("--store-dir", $storeDir) }
      }
    }
  }
  if (Test-Path $bundleStore) { & pnpm install --frozen-lockfile --offline @storeArgs }
  else {
    & pnpm install --frozen-lockfile @storeArgs
    if ($LASTEXITCODE -ne 0) { Info "retrying offline from the pnpm store"; & pnpm install --frozen-lockfile --offline @storeArgs }
  }
  if ($LASTEXITCODE -ne 0) {
    if (Test-Path (Join-Path $WebDir "node_modules\vite")) { Warn "pnpm install failed; using the existing apps/web/node_modules as it is." }
    else { Pop-Location; Fail "Installing the web app's packages failed. Connect to the internet once, or import an offline bundle made with --with-deps." }
  }
  Pop-Location
}

# 4. Docker services ---------------------------------------------------------------
Step "Docker services"
$wantStreaming = ($Preset -eq "streaming") -or ([bool]$key -and $Preset -ne "pocket")
$services = @($CoreServices)
if ($wantStreaming) { $services += "local-stt" }
$built = @{ "local-tts" = "atlas/local-tts:pocket-tts-3.3.0"; "local-stt" = "atlas/local-stt:2-sherpa-onnx-1.13.8" }
foreach ($svc in @($built.Keys)) {
  if ($services -notcontains $svc) { continue }
  docker image inspect $built[$svc] 1>$null 2>$null
  if ($LASTEXITCODE -ne 0) { Info "image $($built[$svc]) is not here yet: it is built now (needs internet; import the offline bundle to skip this)" }
}
$upArgs = @("up", "-d")
if ($Rebuild) { $upArgs += "--build" }
$upArgs += $services
if ($DryRun) { Plan "docker compose $($Profiles -join ' ') $($upArgs -join ' ')" }
else {
  if (-not (Invoke-Compose $upArgs)) { Fail "docker compose up failed (see above). If a port is in use, close that program; if an image is missing and there is no internet, import the offline bundle." }
  $probes = @{ "local-llm" = "http://127.0.0.1:11434/api/version"; "local-speech" = "http://127.0.0.1:8001/health" }
  $limits = @{ "voice-runtime" = 300; "local-tts" = 900; "local-speech" = 180; "local-stt" = 600 }
  $failed = @()
  foreach ($svc in $services) {
    $t = if ($limits.ContainsKey($svc)) { $limits[$svc] } else { 90 }
    $probe = if ($probes.ContainsKey($svc)) { $probes[$svc] } else { "" }
    if (-not (Wait-Service $svc $t $probe)) { $failed += $svc }
  }
  $fatal = @($failed | Where-Object { @("postgres", "redis", "voice-db", "voice-redis", "voice-storage", "voice-runtime") -contains $_ })
  if ($fatal.Count -gt 0) { Fail "required services are not running: $($fatal -join ', ')" }
  if ($failed.Count -gt 0) { Warn "model services not ready: $($failed -join ', '). The test call needs them." }
}

# 5. database ----------------------------------------------------------------------
Step "Database migrations and demo data"
if ($DryRun) { Plan "run 'alembic upgrade head' and 'python -m app.seed' (demo@example.com, password from .env)" }
else {
  Push-Location $ApiDir
  uv run --no-sync alembic upgrade head
  if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "database migration failed" }
  $env:ATLAS_DEMO_PASSWORD = $demoPassword
  uv run --no-sync python -m app.seed
  $seedExit = $LASTEXITCODE
  # Not inherited by the API and worker started below.
  Remove-Item Env:ATLAS_DEMO_PASSWORD -ErrorAction SilentlyContinue
  Pop-Location
  if ($seedExit -ne 0) { Fail "seeding the demo data failed" }
}

# 6. models ------------------------------------------------------------------------
Step "Self-hosted models"
if ($DryRun) {
  Plan "download $LlmModel into Ollama and $SttModel into the speech server if they are missing (needs internet once)"
} else {
  $tags = Get-Json "http://127.0.0.1:11434/api/tags" 10
  $haveLlm = $false
  if ($tags) { $haveLlm = [bool]($tags.models | Where-Object { $_.name -eq $LlmModel -or $_.model -eq $LlmModel }) }
  if ($haveLlm) { Info "$LlmModel is present" }
  elseif (-not $tags) { Warn "the language model server is not answering; skipped the model download" }
  else {
    Info "downloading $LlmModel (about 1 GB, needs internet)"
    docker compose @Profiles exec -T local-llm ollama pull $LlmModel
    if ($LASTEXITCODE -ne 0) { Warn "could not download $LlmModel. Connect to the internet once, or import the offline bundle." }
  }
  if ((Get-HttpStatus "http://127.0.0.1:8001/health") -eq 200) {
    $models = Get-Json "http://127.0.0.1:8001/v1/models" 20
    $haveStt = $false
    if ($models) { $haveStt = [bool]($models.data | Where-Object { $_.id -eq $SttModel }) }
    if ($haveStt) { Info "$SttModel is present" }
    else {
      Info "downloading $SttModel (about 150 MB, needs internet)"
      $code = 0
      try {
        $r = Invoke-WebRequest -Method Post -Uri "http://127.0.0.1:8001/v1/models/$SttModel" -UseBasicParsing -TimeoutSec 900 -ErrorAction Stop
        $code = [int]$r.StatusCode
      } catch { if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } }
      if (@(200, 201, 409) -contains $code) { Info "$SttModel ready" }
      else { Warn "could not download $SttModel (HTTP $code). Connect to the internet once, or import the offline bundle." }
    }
  } else { Warn "the speech server is not answering; skipped the Whisper download" }
  Info "Pocket TTS and live transcription keep their weights in their volumes (downloaded on first start)"
}

# 7. API, worker, web --------------------------------------------------------------
Step "API, worker and web app"
if (-not $DryRun) {
  New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}
$stoppedOwn = $false
foreach ($n in @("web", "worker", "api")) { if (Stop-Own $n) { $stoppedOwn = $true } }
if ($stoppedOwn) { $null = Wait-PortFree 8080 15; $null = Wait-PortFree 5173 15 }

$components = @(
  @{ Name = "api"; Port = 8080; Health = "$ApiUrl/api/v1/health/live" },
  @{ Name = "web"; Port = 5173; Health = "$WebUrl/" }
)
$reuse = @{}
foreach ($c in $components) {
  $owner = Get-PortOwner $c.Port
  if ($owner -eq 0) { continue }
  $pname = Get-ProcessName $owner
  if ($Restart) {
    if ($DryRun) { Plan "stop $pname (pid $owner) holding port $($c.Port) (-Restart)" ; continue }
    Stop-Tree $owner
    if (-not (Wait-PortFree $c.Port)) { Fail "port $($c.Port) is still in use after stopping pid $owner" }
    Info "stopped $pname (pid $owner) that held port $($c.Port) (-Restart)"
  } elseif ((Get-HttpStatus $c.Health) -eq 200) {
    $reuse[$c.Name] = $true
    Info "reusing the $($c.Name) already running on port $($c.Port) ($pname, pid $owner; not started by this script, pass -Restart to replace it)"
  } else {
    Fail "port $($c.Port) is used by $pname (pid $owner) and it is not the ATLAS $($c.Name). Close it, or re-run with -Restart to stop it."
  }
}
$foreignWorker = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match "^(python|arq)" -and $_.CommandLine -like "*app.workers.main.WorkerSettings*" })

$py = Join-Path $ApiDir ".venv\Scripts\python.exe"
if ($DryRun) {
  if (-not $reuse["api"]) { Plan "start the API: $py -m uvicorn app.main:app --port 8080 (log in $LogDir)" }
  if ($foreignWorker.Count -eq 0) { Plan "start the worker: $py -m arq app.workers.main.WorkerSettings" }
  else { Info "a worker is already running (pid $($foreignWorker[0].ProcessId)): it would be reused" }
  if (-not $reuse["web"]) { Plan "start the web app: vite on port 5173" }
} else {
  if (-not $reuse["api"]) {
    $apiPid = Start-Background "api" $py @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8080") $ApiDir
  }
  $ok = $false
  for ($i = 0; $i -lt 60; $i++) {
    $code = Get-HttpStatus "$ApiUrl/api/v1/health/ready" 5
    if ($code -eq 200) { $ok = $true; break }
    if (-not $reuse["api"] -and -not (Get-Process -Id $apiPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Seconds 2
  }
  if (-not $ok) { Show-LogTail "api"; Fail "the API did not become ready on $ApiUrl" }
  Info "API ready: $ApiUrl/docs"

  if ($foreignWorker.Count -gt 0) {
    Info "reusing the worker already running (pid $($foreignWorker[0].ProcessId)); not started by this script"
  } else {
    $workerPid = Start-Background "worker" $py @("-m", "arq", "app.workers.main.WorkerSettings") $ApiDir
    Start-Sleep -Seconds 3
    if (-not (Get-Process -Id $workerPid -ErrorAction SilentlyContinue)) { Show-LogTail "worker"; Warn "the worker stopped right after starting" }
  }

  if (-not $reuse["web"]) {
    $vite = Join-Path $WebDir "node_modules\vite\bin\vite.js"
    $nodeExe = (Get-Command node).Source
    if (Test-Path $vite) { $webPid = Start-Background "web" $nodeExe @("`"$vite`"") $WebDir }
    else { $webPid = Start-Background "web" "cmd.exe" @("/d", "/c", "pnpm", "dev") $WebDir }
    $ok = $false
    for ($i = 0; $i -lt 45; $i++) {
      if ((Get-HttpStatus "$WebUrl/" 5) -eq 200) { $ok = $true; break }
      if (-not (Get-Process -Id $webPid -ErrorAction SilentlyContinue)) { break }
      Start-Sleep -Seconds 2
    }
    if (-not $ok) { Show-LogTail "web"; Fail "the web app did not start on $WebUrl" }
  }
  Info "web app ready: $WebUrl"
}

# 8. connections, preset, agent ------------------------------------------------------
Step "AI providers, speed preset and demo agent"
$activePreset = "private_pocket_tts"
$helper = "..\..\infra\scripts\demo_configure.py"
if ($DryRun) {
  Plan "sign in as demo@example.com and re-use or create the connections 'Local LLM (Ollama)', 'Local speech (Whisper)', 'Pocket TTS (local-tts)'"
  if ($key) { Plan "save 'Live speech (local-stt)' with the given Deepgram key if there is internet (key not shown)" }
  Plan "apply private_streaming if that connection exists or was saved, else private_pocket_tts; check models; publish 'Mortgage lead qualifier'"
} else {
  $env:ATLAS_DEMO_PASSWORD = $demoPassword
  if ($key) { $env:ATLAS_DEMO_DEEPGRAM_KEY = $key }
  $out = Invoke-ApiPython @($helper, "connections")
  Remove-Item Env:ATLAS_DEMO_DEEPGRAM_KEY -ErrorAction SilentlyContinue
  $key = ""
  if ($script:LastPyExit -ne 0) { Fail "configuring the AI provider connections failed (see above)" }
  $streamingReady = [bool]($out | Where-Object { $_ -eq "STREAMING=yes" })
  $target = "private_pocket_tts"
  if ($streamingReady -and $Preset -ne "pocket") {
    $target = "private_streaming"
    if ($services -notcontains "local-stt") {
      if (-not (Invoke-Compose @("up", "-d", "local-stt"))) { $target = "private_pocket_tts" }
    }
    if ($target -eq "private_streaming" -and -not (Wait-Service "local-stt" 600)) {
      Warn "the live transcription server is not ready: using Whisper instead"
      $target = "private_pocket_tts"
    }
  }
  # The preset is applied on the first setup, or when -Preset/-FastStt was given; a re-run
  # keeps the models the organisation chose (e.g. in the web app).
  $finishArgs = @($helper, "finish", "--stt-model", $SttModel, "--preset", $target, "--web", $WebUrl)
  if ($PSBoundParameters.ContainsKey("Preset") -and $Preset -ne "auto") { $finishArgs += "--force" }
  elseif ($FastStt) { $finishArgs += "--force" }
  $out = Invoke-ApiPython $finishArgs
  $line = $out | Where-Object { $_ -like "PRESET=*" } | Select-Object -Last 1
  if ($line) { $activePreset = $line.Substring(7) }
  $sttLine = $out | Where-Object { $_ -like "STT=*" } | Select-Object -Last 1
  if ($sttLine -eq "STT=streaming") { $activePreset = "private_streaming" }
  elseif ($sttLine -like "STT=whisper:?*") { $SttModel = $sttLine.Substring(12); $activePreset = "private_pocket_tts" }
  if ($script:LastPyExit -ne 0) { Warn "the demo agent could not be published (see above); fix it in the web app under Agents" }
}

# 9. warm-up -------------------------------------------------------------------------
Step "Loading the models into memory"
$skip = "kokoro"
if ($activePreset -eq "private_streaming") { $skip = "kokoro,stt" }
if ($SkipWarm) { Info "skipped (-SkipWarm). Before the demo run: .\make.ps1 local-ai-warm" }
elseif ($DryRun) { Plan "run bench_local_models.py --warm-only --skip $skip" }
else {
  $null = Invoke-ApiPython @("..\..\infra\scripts\bench_local_models.py", "--warm-only", "--stt-model", $SttModel, "--skip", $skip)
  if ($script:LastPyExit -ne 0) { Warn "warming the models failed; the first reply of the first call will be slow" }
}

# 10. summary ------------------------------------------------------------------------
Cleanup-Env
Pop-Location
$presetText = @{
  "private_pocket_tts" = "Private self-hosted (Pocket TTS): Whisper speech-to-text, qwen2.5:1.5b, Pocket TTS voice. Works with no internet."
  "private_streaming"  = "Private streaming (self-hosted): live transcription, qwen2.5:1.5b, Pocket TTS voice. Works with no internet."
}
Write-Host ""
if ($DryRun) { Write-Host "Dry run finished. Nothing was changed." -ForegroundColor Green; exit 0 }
Write-Host "ATLAS demo is ready." -ForegroundColor Green
Write-Host "  Active preset: $($presetText[$activePreset])"
Write-Host "  Open:          $WebUrl"
if ($script:GeneratedPassword) {
  Write-Host "  Sign in:       demo@example.com / $($script:GeneratedPassword)   (generated now and saved in .env as ATLAS_DEMO_PASSWORD; shown only this once)"
} else {
  Write-Host "  Sign in:       demo@example.com, password: the ATLAS_DEMO_PASSWORD value in .env"
}
Write-Host "  Test call:     Agents > Mortgage lead qualifier > Test call > Start test call (allow the microphone)"
Write-Host "  Afterwards:    Calls > the newest test call > Response times"
if ($cloningTokenSet) {
  $cloning = Get-HttpStatus "http://127.0.0.1:8002/v1/voices" 10
  if ($cloning -eq 200) { Write-Host "  Voice cloning: ready (AI providers > Voice > Clone a voice; only with the speaker's permission)" }
  else { Write-Host "  Voice cloning: off. The token is saved, but the gated weights did not load: accept the terms at https://huggingface.co/kyutai/pocket-tts with the token's account, then re-run." }
}
Write-Host "  Logs:          $LogDir"
Write-Host "  Stop all:      .\make.ps1 demo-stop"
if ($script:Warnings.Count -gt 0) {
  Write-Host ""
  Write-Host "Warnings ($($script:Warnings.Count)):" -ForegroundColor Yellow
  foreach ($w in $script:Warnings) { Write-Host "  - $w" -ForegroundColor Yellow }
}
exit 0
