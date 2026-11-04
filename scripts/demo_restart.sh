#!/usr/bin/env bash
# R7 demo: start a conversation, restart the agents, continue it.
# Requires `docker compose up -d` to be running.
set -euo pipefail
cd "$(dirname "$0")/.."
THREAD="restart-$(date +%s)"

echo "== 1. Ask for a refund (creates a pending approval) =="
python scripts/chat_client.py "Refund order 1042" --thread "$THREAD"

echo; echo "== 2. Restart BOTH agents (orchestrator + orders agent) =="
docker compose restart orchestrator orders-agent
echo "waiting for health..."
until curl -sf localhost:8002/healthz >/dev/null && curl -sf localhost:8001/healthz >/dev/null; do sleep 1; done

echo; echo "== 3. The conversation is still there after the restart =="
curl -s "localhost:8002/threads/$THREAD" -H "Authorization: Bearer acme-staff-token"; echo

echo; echo "== 4. Approve in the same thread: executes after the restart =="
python scripts/chat_client.py "approve" --thread "$THREAD"
