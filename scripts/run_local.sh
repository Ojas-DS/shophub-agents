#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export LLM_PROVIDER="${LLM_PROVIDER:-fake}" STORE_API_URL=http://localhost:8000
mkdir -p data
trap 'kill 0' EXIT

PORT=8000 python -m shophub.store_api &
sleep 2
PORT=8001 ORDERS_AGENT_PUBLIC_URL=http://localhost:8001/ python -m shophub.orders_agent &
sleep 3
PORT=8002 AGENT_CARD_URLS=http://localhost:8001 python -m shophub.orchestrator &
sleep 3
echo "Ready: chat API on http://localhost:8002  (try: python scripts/chat_client.py -i)"
wait
