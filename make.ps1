<#
.SYNOPSIS
  ATLAS task runner for Windows. Mirrors the Makefile targets.
.EXAMPLE
  .\make.ps1 infra ; .\make.ps1 dev-api ; .\make.ps1 check
#>
param(
  [Parameter(Position = 0)][string]$Target = "help",
  [string]$m = ""
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Api = Join-Path $Root "services\api"
$Web = Join-Path $Root "apps\web"

function Invoke-In([string]$dir, [scriptblock]$block) {
  Push-Location $dir
  try { & $block; if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE)" } }
  finally { Pop-Location }
}

$targets = [ordered]@{
  "infra"     = { docker compose up -d postgres redis }
  "voice-up"   = { docker compose --profile voice up -d }
  "local-ai-up" = { docker compose --profile local-ai up -d local-llm local-speech }
  "local-ai-models" = { & sh (Join-Path $Root "infra/scripts/pull-local-models.sh") }
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
