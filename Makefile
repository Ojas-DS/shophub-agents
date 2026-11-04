.PHONY: install test evals up down logs demo local

install:            ## create venv + install with dev deps
	python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

test:               ## pytest, no API key, no network
	env -u ANTHROPIC_API_KEY LLM_PROVIDER=fake pytest

evals:              ## eval harness, fake LLM, in-process
	env -u ANTHROPIC_API_KEY LLM_PROVIDER=fake python -m evals.run_evals

up:                 ## docker: postgres + store + orders agent + orchestrator
	docker compose up --build -d

down:
	docker compose down -v

logs:
	docker compose logs -f orchestrator orders-agent store-api

demo:               ## R7 restart demo against the docker stack
	./scripts/demo_restart.sh

local:              ## run the 3 services without docker (sqlite)
	./scripts/run_local.sh
