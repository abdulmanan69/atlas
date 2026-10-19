<#
.SYNOPSIS
  Export or import everything the self-hosted ATLAS demo needs as plain files, for a machine
  with slow or no internet (see docs/university-demo.md).

.DESCRIPTION
  export <dir>  Writes into <dir>:
                  images\<image>.tar      one "docker save" per image the demo uses (including
                                          the two locally built model servers)
                  volumes\<volume>.tar    the model volumes (Ollama, Whisper, Pocket TTS,
                                          live transcription), tarred inside a helper container
                  source\atlas-source-<commit>.zip   the committed code (git archive)
                  manifest.tsv, SHA256SUMS, README.txt
                -WithDeps adds deps\*.tar: a Python 3.12 runtime, the uv package cache for the
                  API and the pnpm package store for the web app, so "uv sync" and
                  "pnpm install" work offline. These are for the same OS and CPU type only.
                -WithData adds the ATLAS and voice-service databases, the voice storage and
                  .env, so a machine configured here can be moved as it is. THAT COPIES EVERY
                  ACCOUNT, PASSWORD HASH, API KEY AND SECRET: keep the bundle like a password.
  import <dir>  Verifies the checksums, loads the images and restores the volumes (and, with
                  -WithDeps contents present, unpacks them into .atlas-run\deps). Volumes that
                  already hold data are left alone unless -Force. Data volumes and .env are only
                  restored with -WithData.

.PARAMETER Only
  Export or import only these images/volumes (comma list of names as shown in the plan, e.g.
  redis:7.4-alpine,local_stt_models; "deps" for the code dependencies). Export also accepts
  any other existing image or volume.
.PARAMETER DryRun
  Print the plan and the estimated sizes, change nothing. Alias: -WhatIf.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File infra\scripts\offline_bundle.ps1 export E:\atlas-bundle -DryRun
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File infra\scripts\offline_bundle.ps1 import E:\atlas-bundle
#>
param(
  [Parameter(Position = 0, Mandatory = $true)][ValidateSet("export", "import")][string]$Action,
  [Parameter(Position = 1, Mandatory = $true)][string]$Dir,
  [switch]$WithData,
  [switch]$WithDeps,
  [switch]$Force,
  [Alias("WhatIf")][switch]$DryRun,
  [string[]]$Only = @()
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ApiDir = Join-Path $Root "services\api"
$WebDir = Join-Path $Root "apps\web"
$DepsTarget = Join-Path $Root ".atlas-run\deps"
$Profiles = @("--profile", "voice", "--profile", "local-ai")
# busybox tar is in this image, and the image is part of the bundle: import needs no internet.
$HelperImage = "redis:7.4-alpine"
$ModelVolumeKeys = @("local_llm_models", "local_speech_models", "local_tts_models", "local_stt_models")
$DataVolumeKeys = @("pg_data", "redis_data", "voice_db_data", "voice_redis_data", "voice_storage_data")
$Tar = Join-Path $env:SystemRoot "System32\tar.exe"
if (-not (Test-Path $Tar)) { $Tar = "tar" }
$OnlyList = @($Only | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

function Step([string]$m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Info([string]$m) { Write-Host "    $m" }
function Warn([string]$m) { Write-Host "    WARNING: $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host ""; Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

function Format-Size([double]$bytes) {
  if ($bytes -ge 1GB) { return ("{0:N1} GB" -f ($bytes / 1GB)) }
  if ($bytes -ge 1MB) { return ("{0:N0} MB" -f ($bytes / 1MB)) }
  return ("{0:N0} KB" -f ($bytes / 1KB))
}
function Get-Sha256([string]$path) { return (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLower() }
function Get-SafeName([string]$name) { return ($name -replace "[/:@\\]", "_") }
function Test-Selected([string[]]$names) {
  if ($OnlyList.Count -eq 0) { return $true }
  foreach ($n in $names) { if ($OnlyList -contains $n) { return $true } }
  return $false
}
function Test-Image([string]$name) { docker image inspect $name 1>$null 2>$null; return ($LASTEXITCODE -eq 0) }
function Test-Volume([string]$name) { docker volume inspect $name 1>$null 2>$null; return ($LASTEXITCODE -eq 0) }

function Get-VolumeKb([string]$name) {
  $out = docker run --rm -v "${name}:/vol:ro" --entrypoint du $HelperImage -sk /vol 2>$null
  $m = [regex]::Match(($out -join " "), "^(\d+)")
  if ($m.Success) { return [double]$m.Groups[1].Value }
  return 0
}

function Test-VolumeEmpty([string]$name) {
  $out = docker run --rm -v "${name}:/vol:ro" --entrypoint sh $HelperImage -c "ls -A /vol | head -n 1" 2>$null
  return (-not ($out -join "").Trim())
}

function Get-VolumeUsers([string]$name) {
  return @(docker ps -q --filter "volume=$name" 2>$null | Where-Object { $_ })
}

function Get-VolumeLabels([string]$name) {
  $json = docker volume inspect --format "{{json .Labels}}" $name 2>$null
  if (-not $json -or $json -eq "null") { return "" }
  $obj = $json | ConvertFrom-Json
  $pairs = @()
  foreach ($p in $obj.PSObject.Properties) { $pairs += "$($p.Name)=$($p.Value)" }
  return ($pairs -join ";")
}

function Resolve-VolumeNames {
  # Compose prefixes volume names with the project name; read the real names from compose.
  $map = @{}
  Push-Location $Root
  $json = docker compose @Profiles config --format json 2>$null
  Pop-Location
  if ($LASTEXITCODE -eq 0 -and $json) {
    $cfg = ($json -join "`n") | ConvertFrom-Json
    foreach ($p in $cfg.volumes.PSObject.Properties) { $map[$p.Name] = $p.Value.name }
  }
  foreach ($k in ($ModelVolumeKeys + $DataVolumeKeys)) { if (-not $map.ContainsKey($k)) { $map[$k] = "atlas_$k" } }
  return $map
}

function Test-SavePlatformFlag {
  $help = docker save --help 2>$null
  return [bool](($help -join "`n") -match "--platform")
}

# ------------------------------------------------------------------------------ export
function Invoke-Export {
  Step "Planning the bundle"
  docker info --format "{{.ServerVersion}}" 1>$null 2>$null
  if ($LASTEXITCODE -ne 0) { Fail "Docker is not running." }
  Push-Location $Root
  $images = @(docker compose @Profiles config --images 2>$null | Where-Object { $_ } | Sort-Object -Unique)
  $ok = ($LASTEXITCODE -eq 0)
  Pop-Location
  if (-not $ok -or $images.Count -eq 0) { Fail "could not read the image list from docker compose (is .env present?)" }
  $volNames = Resolve-VolumeNames

  $items = New-Object System.Collections.Generic.List[object]
  foreach ($img in $images) {
    if (-not (Test-Selected @($img))) { continue }
    $items.Add([pscustomobject]@{ Kind = "image"; Name = $img; Key = $img })
  }
  $keys = @($ModelVolumeKeys)
  if ($WithData) { $keys += $DataVolumeKeys }
  foreach ($k in $keys) {
    $kind = if ($DataVolumeKeys -contains $k) { "data-volume" } else { "volume" }
    if (-not (Test-Selected @($k, $volNames[$k]))) { continue }
    $items.Add([pscustomobject]@{ Kind = $kind; Name = $volNames[$k]; Key = $k })
  }
  # "deps" in -Only selects the code dependencies (with -WithDeps).
  $depsWanted = $WithDeps -and ($OnlyList.Count -eq 0 -or $OnlyList -contains "deps")
  # Explicit extras (any existing image or volume), e.g. for a quick test.
  foreach ($o in $OnlyList) {
    if ($o -eq "deps") { continue }
    if ($items | Where-Object { $_.Name -eq $o -or $_.Key -eq $o }) { continue }
    if (Test-Image $o) { $items.Add([pscustomobject]@{ Kind = "image"; Name = $o; Key = $o }) }
    elseif (Test-Volume $o) { $items.Add([pscustomobject]@{ Kind = "volume"; Name = $o; Key = $o }) }
    else { Warn "'$o' is neither an image nor a volume here: skipped" }
  }

  if (-not (Test-Image $HelperImage)) { Fail "$HelperImage is needed to read the volumes; run the demo setup once (it pulls it)" }
  $total = 0.0
  $missing = @()
  Write-Host ""
  Write-Host ("    {0,-12} {1,-48} {2,10}" -f "KIND", "NAME", "ESTIMATE")
  foreach ($it in $items) {
    $size = 0.0
    if ($it.Kind -eq "image") {
      if (Test-Image $it.Name) { $size = [double](docker image inspect --format "{{.Size}}" $it.Name 2>$null) }
      else { $missing += $it.Name }
    } else {
      if (Test-Volume $it.Name) { $size = (Get-VolumeKb $it.Name) * 1KB }
      else { $missing += $it.Name }
    }
    $it | Add-Member -NotePropertyName Estimate -NotePropertyValue $size
    $total += $size
    $label = if ($size -gt 0) { Format-Size $size } else { "missing" }
    Write-Host ("    {0,-12} {1,-48} {2,10}" -f $it.Kind, $it.Name, $label)
  }
  if ($depsWanted) {
    Write-Host ("    {0,-12} {1,-48} {2,10}" -f "deps", "Python 3.12 + uv cache + pnpm store", "~0.6 GB")
    $total += 0.6GB
  }
  if ($OnlyList.Count -eq 0) {
    Write-Host ("    {0,-12} {1,-48} {2,10}" -f "source", "git archive of HEAD", "~0.1 GB")
    $total += 0.1GB
  }
  Write-Host ("    {0,-12} {1,-48} {2,10}" -f "", "total (image sizes are as stored by Docker)", (Format-Size $total))
  if ($missing.Count -gt 0) {
    Warn "not on this machine, so not exported: $($missing -join ', '). Run the demo setup once (it downloads and builds them), then export again."
  }
  if ($WithData) {
    Warn "-WithData copies the databases, voice storage and .env: every account, password hash, API key and secret. Keep the bundle like a password and import it together with that .env."
  }

  $target = [System.IO.Path]::GetFullPath($Dir)
  $qual = Split-Path -Qualifier $target -ErrorAction SilentlyContinue
  if ($qual -and (Test-Path "$qual\")) {
    $free = (New-Object System.IO.DriveInfo($qual)).AvailableFreeSpace
    Info "free space on ${qual} $(Format-Size $free)"
    if ($free -lt $total * 1.1) { Warn "the bundle may not fit on $qual" }
  }
  if ($DryRun) { Write-Host ""; Write-Host "Dry run: nothing was written." -ForegroundColor Green; return }

  foreach ($sub in @("images", "volumes", "deps", "env", "source")) { New-Item -ItemType Directory -Force -Path (Join-Path $target $sub) | Out-Null }
  $rows = New-Object System.Collections.Generic.List[string]
  $platformFlag = Test-SavePlatformFlag

  Step "Saving Docker images"
  foreach ($it in ($items | Where-Object { $_.Kind -eq "image" -and $_.Estimate -gt 0 })) {
    $rel = "images\" + (Get-SafeName $it.Name) + ".tar"
    $file = Join-Path $target $rel
    $plat = (docker image inspect --format "{{.Os}}/{{.Architecture}}" $it.Name 2>$null)
    if ((Test-Path $file) -and -not $Force) { Info "$($it.Name): already in the bundle" }
    else {
      Info "$($it.Name) -> $rel"
      $saveArgs = @("save", "-o", "$file.partial")
      if ($platformFlag -and $plat) { $saveArgs += @("--platform", $plat) }
      $saveArgs += $it.Name
      & docker @saveArgs
      if ($LASTEXITCODE -ne 0) { Remove-Item "$file.partial" -ErrorAction SilentlyContinue; Fail "docker save $($it.Name) failed" }
      Move-Item -Force "$file.partial" $file
    }
    $rows.Add("image`t$($it.Name)`t$rel`t$((Get-Item $file).Length)`t$(Get-Sha256 $file)`t$plat")
  }

  $vols = @($items | Where-Object { $_.Kind -ne "image" -and $_.Estimate -ge 0 -and (Test-Volume $_.Name) })
  if ($vols.Count -gt 0) {
    Step "Saving Docker volumes"
    $stopped = @()
    foreach ($it in ($vols | Where-Object { $_.Kind -eq "data-volume" })) {
      foreach ($c in (Get-VolumeUsers $it.Name)) { if ($stopped -notcontains $c) { $stopped += $c } }
    }
    if ($stopped.Count -gt 0) {
      Info "stopping $($stopped.Count) container(s) that use the data volumes, for a consistent copy"
      docker stop @stopped | Out-Null
    }
    $volDir = Join-Path $target "volumes"
    foreach ($it in $vols) {
      $rel = "volumes\$($it.Name).tar"
      $file = Join-Path $target $rel
      if ((Test-Path $file) -and -not $Force) { Info "$($it.Name): already in the bundle" }
      else {
        Info "$($it.Name) -> $rel"
        docker run --rm -v "$($it.Name):/vol:ro" -v "${volDir}:/out" --entrypoint tar $HelperImage -cf "/out/$($it.Name).tar.partial" -C /vol .
        if ($LASTEXITCODE -ne 0) { Remove-Item "$file.partial" -ErrorAction SilentlyContinue; Fail "saving volume $($it.Name) failed" }
        Move-Item -Force "$file.partial" $file
      }
      $rows.Add("$($it.Kind)`t$($it.Name)`t$rel`t$((Get-Item $file).Length)`t$(Get-Sha256 $file)`t$(Get-VolumeLabels $it.Name)")
    }
    if ($stopped.Count -gt 0) { docker start @stopped | Out-Null; Info "restarted those containers" }
  }

  if ($WithData) {
    Step "Settings file"
    $envFile = Join-Path $Root ".env"
    if (Test-Path $envFile) {
      $rel = "env\dot-env"
      Copy-Item -Force $envFile (Join-Path $target $rel)
      $f = Join-Path $target $rel
      $rows.Add("env`t.env`t$rel`t$((Get-Item $f).Length)`t$(Get-Sha256 $f)`t")
      Info "copied .env (contains secrets)"
    } else { Warn "no .env here: the data volumes are useless without the .env that created them" }
  }

  if ($depsWanted) { Export-Deps $target $rows }

  if ($OnlyList.Count -eq 0 -and (Get-Command git -ErrorAction SilentlyContinue)) {
    Step "Source code"
    $sha = (git -C $Root rev-parse --short HEAD 2>$null)
    if ($sha) {
      $rel = "source\atlas-source-$sha.zip"
      $f = Join-Path $target $rel
      git -C $Root archive --format=zip -o $f HEAD
      if ($LASTEXITCODE -eq 0) {
        $rows.Add("source`tatlas-source-$sha`t$rel`t$((Get-Item $f).Length)`t$(Get-Sha256 $f)`t")
        $dirty = git -C $Root status --porcelain 2>$null
        if ($dirty) { Warn "the zip holds commit $sha only; uncommitted changes are not in it" }
        Info "commit $sha -> $rel"
      } else { Warn "git archive failed: copy the repository yourself" }
    }
  }

  Write-Manifest $target $rows
}

function Export-Deps([string]$target, $rows) {
  Step "Code dependencies (same OS and CPU type only)"
  $work = Join-Path $target "deps\work"
  if (Test-Path $work) { Remove-Tree $work }
  $pyDir = Join-Path $work "uv-python"; $cache = Join-Path $work "uv-cache"; $store = Join-Path $work "pnpm-store"
  $venv = Join-Path $work "venv"; $web = Join-Path $work "web"
  New-Item -ItemType Directory -Force -Path $pyDir, $cache, $store, $web | Out-Null
  $saved = @{}
  foreach ($k in @("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PROJECT_ENVIRONMENT")) { $saved[$k] = [Environment]::GetEnvironmentVariable($k, "Process") }
  try {
    Info "Python 3.12 runtime (downloaded now)"
    uv python install 3.12 --install-dir $pyDir --no-bin --no-registry
    if ($LASTEXITCODE -ne 0) { Fail "uv python install failed (needs internet)" }
    Info "API packages into a fresh uv cache (a throwaway environment; services\api\.venv is not touched)"
    $env:UV_CACHE_DIR = $cache; $env:UV_PYTHON_INSTALL_DIR = $pyDir; $env:UV_PROJECT_ENVIRONMENT = $venv
    Push-Location $ApiDir
    uv sync --frozen --managed-python
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Fail "uv sync into the bundle cache failed (needs internet)" }
  } finally {
    foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], "Process") }
  }
  Info "web packages into a fresh pnpm store (from a copy of the lockfile; apps\web is not touched)"
  foreach ($f in @("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", ".npmrc")) {
    $src = Join-Path $WebDir $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $web $f) }
  }
  Push-Location $web
  pnpm fetch --store-dir $store
  $code = $LASTEXITCODE
  Pop-Location
  if ($code -ne 0) { Fail "pnpm fetch into the bundle store failed (needs internet)" }
  # uv links the minor version (cpython-3.12-...) to the patch version with a junction;
  # a tar of a junction is not portable, and uv finds the patch version without it.
  Get-ChildItem $pyDir -Directory -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
    ForEach-Object { cmd /c rmdir "$($_.FullName)" | Out-Null }
  foreach ($pair in @(@("uv-python", $pyDir), @("uv-cache", $cache), @("pnpm-store", $store))) {
    $rel = "deps\$($pair[0]).tar"
    $f = Join-Path $target $rel
    & $Tar -cf $f -C $pair[1] .
    if ($LASTEXITCODE -ne 0) { Fail "packing $($pair[0]) failed" }
    $rows.Add("deps`t$($pair[0])`t$rel`t$((Get-Item $f).Length)`t$(Get-Sha256 $f)`twindows-$($env:PROCESSOR_ARCHITECTURE.ToLower())")
    Info "$($pair[0]) -> $rel ($(Format-Size (Get-Item $f).Length))"
  }
  Remove-Tree $work
}

function Remove-Tree([string]$path) {
  # Remove junctions first so their targets are never followed, then the rest.
  Get-ChildItem $path -Recurse -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
    ForEach-Object { cmd /c rmdir "$($_.FullName)" 2>$null | Out-Null }
  cmd /c rmdir /s /q "$path" 2>$null | Out-Null
  # A virus scanner can hold a just-emptied folder for a moment.
  for ($i = 0; $i -lt 5 -and (Test-Path $path); $i++) {
    Start-Sleep -Seconds 2
    Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue
  }
}

function Write-Manifest([string]$target, $rows) {
  $manifest = Join-Path $target "manifest.tsv"
  $old = @()
  if ((Test-Path $manifest) -and $OnlyList.Count -gt 0) {
    # A partial export adds to an existing bundle instead of replacing its manifest.
    $names = @($rows | ForEach-Object { ($_ -split "`t")[1] })
    $old = @(Get-Content $manifest | Where-Object { $_ -and -not $_.StartsWith("#") -and -not $_.StartsWith("meta") } |
      Where-Object { $names -notcontains (($_ -split "`t")[1]) })
  }
  $commit = (git -C $Root rev-parse --short HEAD 2>$null)
  # The version that really fills the store: in apps\web a newer pnpm runs the pinned one.
  Push-Location $WebDir
  $pnpmV = (pnpm --version 2>$null)
  Pop-Location
  $lines = @(
    "#atlas-offline-bundle`t1",
    "#kind`tname`tfile`tbytes`tsha256`textra",
    "meta`tcreated`t$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))",
    "meta`tplatform`twindows-$($env:PROCESSOR_ARCHITECTURE.ToLower())",
    "meta`tcommit`t$commit",
    "meta`tpnpm`t$pnpmV",
    "meta`twith_data`t$(if ($WithData) { 'yes' } else { 'no' })"
  ) + $old + @($rows)
  $utf8 = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($manifest, (($lines -join "`n") + "`n"), $utf8)
  $sums = @($lines | Where-Object { $_ -and -not $_.StartsWith("#") -and -not $_.StartsWith("meta") } |
    ForEach-Object { $c = $_ -split "`t"; "$($c[4])  $($c[2] -replace '\\', '/')" })
  [System.IO.File]::WriteAllText((Join-Path $target "SHA256SUMS"), (($sums -join "`n") + "`n"), $utf8)
  $readme = @(
    "ATLAS offline bundle (made $((Get-Date).ToString('yyyy-MM-dd')) from commit $commit)",
    "",
    "1. Unzip source\atlas-source-*.zip (or clone the repository) on the demo machine.",
    "2. Import:  powershell -NoProfile -ExecutionPolicy Bypass -File infra\scripts\offline_bundle.ps1 import <this folder>",
    "            (Linux/macOS: bash infra/scripts/offline_bundle.sh import <this folder>)",
    "3. Set up:  .\make.ps1 demo     (Linux/macOS: make demo)",
    "",
    "manifest.tsv lists every file with its size and SHA-256; SHA256SUMS works with 'sha256sum -c'.",
    "See docs/university-demo.md in the repository."
  )
  if ($WithData) { $readme += @("", "THIS BUNDLE CONTAINS DATABASES AND .env: ACCOUNTS, PASSWORD HASHES, API KEYS AND SECRETS.") }
  [System.IO.File]::WriteAllText((Join-Path $target "README.txt"), (($readme -join "`r`n") + "`r`n"), $utf8)
  $count = @($lines | Where-Object { $_ -and -not $_.StartsWith("#") -and -not $_.StartsWith("meta") }).Count
  $bytes = 0.0
  foreach ($l in $lines) { $c = $l -split "`t"; if ($c.Count -ge 4 -and $c[3] -match "^\d+$") { $bytes += [double]$c[3] } }
  Write-Host ""
  Write-Host "Bundle written to $target ($count files, $(Format-Size $bytes)). Manifest: manifest.tsv" -ForegroundColor Green
}

# ------------------------------------------------------------------------------ import
function Read-Manifest([string]$target) {
  $manifest = Join-Path $target "manifest.tsv"
  if (-not (Test-Path $manifest)) { Fail "no manifest.tsv in $target" }
  $rows = @()
  $meta = @{}
  foreach ($l in (Get-Content $manifest)) {
    if (-not $l -or $l.StartsWith("#")) { continue }
    $c = $l -split "`t"
    if ($c[0] -eq "meta") { $meta[$c[1]] = $c[2]; continue }
    $rows += [pscustomobject]@{ Kind = $c[0]; Name = $c[1]; File = $c[2]; Bytes = [double]$c[3]; Sha = $c[4]; Extra = $(if ($c.Count -gt 5) { $c[5] } else { "" }) }
  }
  return @{ Rows = $rows; Meta = $meta }
}

function Invoke-Import {
  $target = [System.IO.Path]::GetFullPath($Dir)
  $m = Read-Manifest $target
  $rows = @($m.Rows | Where-Object { Test-Selected @($_.Name, ($_.Name -replace "^[^_]+_", "")) })
  docker info --format "{{.ServerVersion}}" 1>$null 2>$null
  if ($LASTEXITCODE -ne 0) { Fail "Docker is not running. Start Docker Desktop and re-run." }
  Info "bundle from commit $($m.Meta['commit']), made $($m.Meta['created']) on $($m.Meta['platform'])"
  if ($m.Meta["with_data"] -eq "yes" -and -not $WithData) {
    Info "this bundle also holds databases and .env; they are restored only with -WithData"
  }

  Step "Checking the files"
  $bad = @()
  foreach ($r in $rows) {
    $f = Join-Path $target $r.File
    if (-not (Test-Path $f)) { $bad += "$($r.File) is missing"; continue }
    if ((Get-Item $f).Length -ne $r.Bytes) { $bad += "$($r.File) has the wrong size"; continue }
    if ($DryRun) { continue }
    if ((Get-Sha256 $f) -ne $r.Sha) { $bad += "$($r.File) is damaged (SHA-256 differs)" }
  }
  if ($bad.Count -gt 0) { Fail ("bundle check failed:`n  " + ($bad -join "`n  ")) }
  if ($DryRun) { Info "all $($rows.Count) files are present with the right size (checksums are verified on a real import)" }
  else { Info "all $($rows.Count) files verified (size and SHA-256)" }

  $imgs = @($rows | Where-Object { $_.Kind -eq "image" } | Sort-Object { if ($_.Name -eq $HelperImage) { 0 } else { 1 } })
  if ($imgs.Count -gt 0) { Step "Docker images" }
  foreach ($r in $imgs) {
    if ((Test-Image $r.Name) -and -not $Force) { Info "$($r.Name): already here"; continue }
    if ($DryRun) { Info "[dry run] would load $($r.File) ($(Format-Size $r.Bytes))"; continue }
    Info "loading $($r.Name) ($(Format-Size $r.Bytes))"
    docker load -i (Join-Path $target $r.File) | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail "docker load $($r.File) failed" }
  }

  $vols = @($rows | Where-Object { $_.Kind -eq "volume" -or ($_.Kind -eq "data-volume" -and $WithData) })
  if ($vols.Count -gt 0) {
    Step "Docker volumes"
    if (-not (Test-Image $HelperImage) -and -not $DryRun) { Fail "$HelperImage is needed to restore volumes; import the images first" }
  }
  foreach ($r in $vols) {
    $exists = Test-Volume $r.Name
    if ($exists -and -not (Test-VolumeEmpty $r.Name)) {
      if (-not $Force) { Info "$($r.Name): already holds data, left alone (use -Force to replace it)"; continue }
      if ((Get-VolumeUsers $r.Name).Count -gt 0) { Fail "$($r.Name) is in use by a running container. Stop the demo first: .\make.ps1 demo-stop" }
    }
    if ($DryRun) { Info "[dry run] would restore $($r.Name) from $($r.File) ($(Format-Size $r.Bytes))"; continue }
    if (-not $exists) {
      $labelArgs = @()
      foreach ($p in ($r.Extra -split ";")) { if ($p) { $labelArgs += @("--label", $p) } }
      docker volume create @labelArgs $r.Name | Out-Null
      if ($LASTEXITCODE -ne 0) { Fail "could not create volume $($r.Name)" }
    } else {
      docker run --rm -v "$($r.Name):/vol" --entrypoint sh $HelperImage -c "find /vol -mindepth 1 -maxdepth 1 -exec rm -rf {} +"
    }
    $inDir = Join-Path $target (Split-Path -Parent $r.File)
    $leaf = Split-Path -Leaf $r.File
    Info "restoring $($r.Name) ($(Format-Size $r.Bytes))"
    docker run --rm -v "$($r.Name):/vol" -v "${inDir}:/in:ro" --entrypoint tar $HelperImage -xf "/in/$leaf" -C /vol
    if ($LASTEXITCODE -ne 0) { Fail "restoring $($r.Name) failed" }
  }

  $envRow = @($rows | Where-Object { $_.Kind -eq "env" }) | Select-Object -First 1
  if ($envRow -and $WithData) {
    Step "Settings file (.env)"
    $src = Join-Path $target $envRow.File
    $dst = Join-Path $Root ".env"
    if (-not (Test-Path $dst)) {
      if ($DryRun) { Info "[dry run] would copy the bundle's .env" } else { Copy-Item $src $dst; Info "copied .env from the bundle" }
    } elseif ((Get-Sha256 $dst) -eq $envRow.Sha) { Info ".env is already the bundle's" }
    elseif ($Force) {
      $backup = Join-Path $Root (".env.backup-" + (Get-Date).ToString("yyyyMMdd-HHmmss"))
      if ($DryRun) { Info "[dry run] would back up .env to $backup and copy the bundle's" }
      else { Copy-Item $dst $backup; Copy-Item -Force $src $dst; Info "replaced .env (old one: $backup)" }
    } else {
      Warn ".env differs from the bundle's. The restored databases only work with the bundle's .env: re-run with -WithData -Force to replace it (the current one is backed up)."
    }
  }

  $deps = @($rows | Where-Object { $_.Kind -eq "deps" })
  if ($deps.Count -gt 0) {
    Step "Code dependencies"
    $here = "windows-$($env:PROCESSOR_ARCHITECTURE.ToLower())"
    if ($m.Meta["platform"] -ne $here) { Warn "the bundle's code dependencies were made on $($m.Meta['platform']), this machine is ${here}: they will not work here; setup needs internet for them" }
    # Outside apps\web: there a newer pnpm would try to download the pinned version.
    Push-Location $Root
    $pnpmHere = (pnpm --version 2>$null)
    Pop-Location
    if ($pnpmHere -and $m.Meta["pnpm"] -and ((($pnpmHere -split "\.")[0]) -ne (($m.Meta["pnpm"] -split "\.")[0]))) {
      Warn "the pnpm store was made with pnpm $($m.Meta['pnpm']), this machine has pnpm ${pnpmHere}: install the same major version for an offline install"
    }
    foreach ($r in $deps) {
      $dest = Join-Path $DepsTarget $r.Name
      if ((Test-Path $dest) -and -not $Force) { Info "$($r.Name): already unpacked in $dest"; continue }
      if ($DryRun) { Info "[dry run] would unpack $($r.File) into $dest"; continue }
      if (Test-Path $dest) { Remove-Tree $dest }
      New-Item -ItemType Directory -Force -Path $dest | Out-Null
      & $Tar -xf (Join-Path $target $r.File) -C $dest
      if ($LASTEXITCODE -ne 0) { Fail "unpacking $($r.File) failed" }
      Info "$($r.Name) -> $dest"
    }
  }

  Write-Host ""
  if ($DryRun) { Write-Host "Dry run: nothing was changed." -ForegroundColor Green }
  else { Write-Host "Import finished. Next: .\make.ps1 demo" -ForegroundColor Green }
}

if ($Action -eq "export") { Invoke-Export } else { Invoke-Import }
exit 0
