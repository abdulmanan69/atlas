# ATLAS task runner (Linux/macOS/CI). On Windows use: .\make.ps1 <target>
API := services/api
WEB := apps/web

.PHONY: help infra local-ai-up local-ai-models voice-up voice-down voice-logs up down dev-api dev-web worker migrate migration seed test lint format typecheck check build

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | tr '\n' ' '; echo

infra:            ## Start postgres + redis
	docker compose up -d postgres redis

voice-up:         ## Start the internal voice runtime (+ its db and storage)
	docker compose --profile voice up -d

local-ai-up:      ## Start self-hosted models (Ollama + Whisper/Kokoro), CPU
	docker compose --profile local-ai up -d local-llm local-speech

local-ai-models:  ## Download the default small local models (about 2 GB, once)
	sh infra/scripts/pull-local-models.sh

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
