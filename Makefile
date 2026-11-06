.PHONY: install test evals up down logs demo local

install:
	python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

test:
	env -u ANTHROPIC_API_KEY LLM_PROVIDER=fake pytest

evals:
	env -u ANTHROPIC_API_KEY LLM_PROVIDER=fake python -m evals.run_evals

up:
	docker compose up --build -d

down:
	docker compose down -v

logs:
	docker compose logs -f orchestrator orders-agent store-api

demo:
	./scripts/demo_restart.sh

local:
	./scripts/run_local.sh
