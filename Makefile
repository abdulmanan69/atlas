# ATLAS task runner (Linux/macOS/CI). On Windows use: .\make.ps1 <target>
API := services/api
WEB := apps/web

.PHONY: help infra local-ai-up local-tts-up local-stt-up local-ai-models local-ai-warm local-ai-bench local-ai-down voice-up voice-down voice-logs up down dev-api dev-web worker migrate migration seed test lint format typecheck check build

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | tr '\n' ' '; echo

infra:            ## Start postgres + redis
	docker compose up -d postgres redis

voice-up:         ## Start the internal voice runtime (+ its db and storage)
	docker compose --profile voice up -d

local-ai-up:      ## Start self-hosted models (Ollama + Whisper/Kokoro), CPU
	docker compose --profile local-ai up -d local-llm local-speech

local-tts-up:     ## Build and start Pocket TTS (OpenAI-compatible voice on 127.0.0.1:8002), CPU
	docker compose --profile local-ai up -d --build local-tts

local-stt-up:     ## Build and start live English speech-to-text (Deepgram protocol on 127.0.0.1:8003), CPU
	docker compose --profile local-ai up -d --build local-stt

local-ai-models:  ## Download the default small local models (about 1.5 GB, once)
	sh infra/scripts/pull-local-models.sh

local-ai-warm:    ## Load every local model into memory so the first call is not a cold start
	cd services/api && uv run python ../../infra/scripts/bench_local_models.py --warm-only

local-ai-bench:   ## Measure local STT/TTS/LLM latency (needs about 3.5 GB free RAM)
	cd services/api && uv run python ../../infra/scripts/bench_local_models.py

local-ai-down:    ## Stop the self-hosted model containers (frees their RAM)
	docker compose --profile local-ai stop local-llm local-speech local-tts local-stt

voice-down:
	docker compose --profile voice stop voice-runtime voice-storage voice-db

voice-logs:
	docker compose --profile voice logs -f --tail=100 voice-runtime

up:               ## Full stack in containers
	docker compose --profile app up -d --build

down:
	docker compose --profile app down

dev-api: infra
	cd $(API) && uv run uvicorn app.main:app --reload --port 8080

dev-web:
	cd $(WEB) && pnpm dev

worker:
	cd $(API) && uv run arq app.workers.main.WorkerSettings

migrate:
	cd $(API) && uv run alembic upgrade head

migration:        ## make migration m="describe change"
	cd $(API) && uv run alembic revision --autogenerate -m "$(m)"

seed: migrate
	cd $(API) && uv run python -m app.seed

test:
	cd $(API) && uv run pytest -q
	cd $(WEB) && pnpm test --run

lint:
	cd $(API) && uv run ruff check app tests && uv run ruff format --check app tests
	cd $(WEB) && pnpm lint

format:
	cd $(API) && uv run ruff format app tests && uv run ruff check app tests --fix

typecheck:
	cd $(API) && uv run mypy app
	cd $(WEB) && pnpm typecheck

build:
	cd $(WEB) && pnpm build

check: lint typecheck test build
	cd $(API) && uv run alembic check
