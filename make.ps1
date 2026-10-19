<#
.SYNOPSIS
  ATLAS task runner for Windows. Mirrors the Makefile targets.
.EXAMPLE
  .\make.ps1 infra ; .\make.ps1 dev-api ; .\make.ps1 check
.EXAMPLE
  .\make.ps1 demo ; .\make.ps1 demo -DryRun ; .\make.ps1 demo -Restart ; .\make.ps1 demo-stop
#>
param(
  [Parameter(Position = 0)][string]$Target = "help",
  [string]$m = "",
  # Extra options for "demo" (e.g. -Restart, -DryRun), passed on to setup_local_demo.ps1.
  [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Api = Join-Path $Root "services\api"
$Web = Join-Path $Root "apps\web"

# "-Name value" / "-Switch" tokens -> hashtable, so they bind as named parameters.
function ConvertTo-Splat([object[]]$tokens) {
  $named = @{}
  for ($i = 0; $i -lt $tokens.Count; $i++) {
    $t = [string]$tokens[$i]
    if ($t -notmatch '^-(\w+)$') { continue }
    $next = if ($i + 1 -lt $tokens.Count) { [string]$tokens[$i + 1] } else { "" }
    if ($next -and -not $next.StartsWith("-")) { $named[$Matches[1]] = $next; $i++ }
    else { $named[$Matches[1]] = $true }
  }
  return $named
}
$DemoArgs = ConvertTo-Splat $Rest
$DemoScript = Join-Path $Root "infra\scripts\setup_local_demo.ps1"

function Invoke-In([string]$dir, [scriptblock]$block) {
  Push-Location $dir
  try { & $block; if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE)" } }
  finally { Pop-Location }
}

$targets = [ordered]@{
  "infra"     = { docker compose up -d postgres redis }
  "voice-up"   = { docker compose --profile voice up -d }
  "local-ai-up" = { docker compose --profile local-ai up -d local-llm local-speech }
  "local-tts-up" = { docker compose --profile local-ai up -d --build local-tts }
  "local-stt-up" = { docker compose --profile local-ai up -d --build local-stt }
  "local-ai-models" = { & sh (Join-Path $Root "infra/scripts/pull-local-models.sh") }
  "local-ai-warm" = { Invoke-In $Api { uv run python ../../infra/scripts/bench_local_models.py --warm-only } }
  "local-ai-bench" = { Invoke-In $Api { uv run python ../../infra/scripts/bench_local_models.py } }
  "demo"      = { & $DemoScript @DemoArgs }
  "demo-stop" = { & $DemoScript -Stop }
  "local-ai-down" = { docker compose --profile local-ai stop local-llm local-speech local-tts local-stt }
  # Scripted test call (docs/testing-voice.md); options pass through: .\make.ps1 e2e-call --runs 2
  "e2e-call"  = {
    $py = Join-Path $Api ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { $py = "python" }
    & $py (Join-Path $Root "infra\scripts\e2e_test_call.py") @Rest
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
  }
  "voice-down" = { docker compose --profile voice stop voice-runtime voice-storage voice-db }
  "voice-logs" = { docker compose --profile voice logs -f --tail=100 voice-runtime }
  "up"        = { docker compose --profile app up -d --build }
  "down"      = { docker compose --profile app down }
  "dev-api"   = { & $targets["infra"]; Invoke-In $Api { uv run uvicorn app.main:app --reload --port 8080 } }
  "dev-web"   = { Invoke-In $Web { pnpm dev } }
  "worker"    = { Invoke-In $Api { uv run arq app.workers.main.WorkerSettings } }
  "migrate"   = { Invoke-In $Api { uv run alembic upgrade head } }
  "migration" = { Invoke-In $Api { uv run alembic revision --autogenerate -m $m } }
  "seed"      = { & $targets["migrate"]; Invoke-In $Api { uv run python -m app.seed } }
  "test"      = { Invoke-In $Api { uv run pytest -q }; Invoke-In $Web { pnpm test --run } }
  "lint"      = { Invoke-In $Api { uv run ruff check app tests; uv run ruff format --check app tests }; Invoke-In $Web { pnpm lint } }
  "format"    = { Invoke-In $Api { uv run ruff format app tests; uv run ruff check app tests --fix } }
  "typecheck" = { Invoke-In $Api { uv run mypy app }; Invoke-In $Web { pnpm typecheck } }
  "build"     = { Invoke-In $Web { pnpm build } }
  "check"     = { foreach ($t in "lint", "typecheck", "test", "build") { & $targets[$t] }; Invoke-In $Api { uv run alembic check } }
}

if ($Target -eq "help" -or -not $targets.Contains($Target)) {
  "Targets: " + ($targets.Keys -join ", ")
  exit 0
}
& $targets[$Target]
