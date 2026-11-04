"""R9 - mini evaluation harness. Reports PASS/FAIL per case (no blended score).

Default: fake LLM, whole stack in-process, fresh store per case. No key, no network.
    python -m evals.run_evals

Against a running stack (e.g. docker compose with LLM_PROVIDER=anthropic):
    python -m evals.run_evals --base-url http://localhost:8002 --store-url http://localhost:8000
  (cases mutate orders, so run that against a freshly started stack.)
"""

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

TOKENS = {"acme": "Bearer acme-staff-token", "bolt": "Bearer bolt-staff-token",
          "auditor": "Bearer platform-auditor-token"}
CASES = Path(__file__).with_name("cases.json")


def parse_sse(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


async def check_turn(chat_client: httpx.AsyncClient, store_client: httpx.AsyncClient, store_url: str,
                     thread: str, turn: dict[str, Any]) -> list[str]:
    """Run one turn; return a list of failure reasons (empty = pass)."""
    exp, failures = turn["expect"], []
    resp = await chat_client.post("/chat", json={"message": turn["message"], "thread_id": thread},
                                  headers={"Authorization": TOKENS[turn["token"]]})
    if "http_status" in exp:
        if resp.status_code != exp["http_status"]:
            failures.append(f"http {resp.status_code} != {exp['http_status']}")
        return failures
    if resp.status_code != 200:
        return [f"http {resp.status_code}"]

    events = parse_sse(resp.text)
    final = next((d for e, d in events if e == "final"), {"text": "", "awaiting_approval": False})
    text = final["text"]
    tools = {d.get("tool") for e, d in events if e == "remote_status"}
    proposed = any(e == "proposal" for e, _ in events)

    for t in exp.get("tools", []):
        if t not in tools:
            failures.append(f"tool {t} not called (saw {sorted(x for x in tools if x)})")
    for s in exp.get("contains", []):
        if s.lower() not in text.lower():
            failures.append(f"answer missing {s!r}")
    for s in exp.get("not_contains", []):
        if s.lower() in text.lower():
            failures.append(f"answer must not contain {s!r}")
    if "proposal" in exp and proposed != exp["proposal"]:
        failures.append(f"proposal raised={proposed}, expected {exp['proposal']}")
    if st := exp.get("store"):
        r = await store_client.get(f"{store_url}/orders/lookup", params={"id": st["order"]},
                                   headers={"Authorization": TOKENS[st.get("token", "acme")]})
        actual = r.json().get("order", {}).get("status")
        if actual != st["status"]:
            failures.append(f"store: order {st['order']} is {actual}, expected {st['status']}")
    if failures:
        failures.append(f"answer was: {text[:160]!r}")
    return failures


async def run_case(case: dict, base_url: str | None, store_url: str | None) -> tuple[bool, list[str], float]:
    from shophub.testing import running_stack

    start = time.perf_counter()
    thread = f"eval-{case['id']}-{uuid.uuid4().hex[:6]}"
    async with contextlib.AsyncExitStack() as stack:
        if base_url:
            chat = await stack.enter_async_context(httpx.AsyncClient(base_url=base_url, timeout=120))
            store, s_url = chat, store_url
        else:
            s = await stack.enter_async_context(running_stack())  # fresh store + agents per case
            chat, store, s_url = s.client, s.http, s.settings.store_api_url
        failures: list[str] = []
        for i, turn in enumerate(case["turns"], 1):
            for f in await check_turn(chat, store, s_url, thread, turn):
                failures.append(f"turn {i}: {f}")
            if failures:
                break
    return not failures, failures, time.perf_counter() - start


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", help="chat API of a running stack (default: in-process, fake LLM)")
    ap.add_argument("--store-url", default="http://localhost:8000")
    ap.add_argument("--only", help="run a single case id")
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)  # keep the report readable

    cases = [c for c in json.loads(CASES.read_text()) if not args.only or c["id"] == args.only]
    mode = f"live stack at {args.base_url}" if args.base_url else "in-process, fake LLM"
    print(f"\nShopHub agent evals - {len(cases)} cases - {mode}\n")
    print(f"{'RESULT':<7} {'CASE':<34} {'CATEGORY':<32} {'TIME':>6}")
    print("-" * 82)
    passed = 0
    for case in cases:
        ok, failures, secs = await run_case(case, args.base_url, args.store_url if args.base_url else None)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL':<7} {case['id']:<34} {case['category']:<32} {secs:>5.2f}s")
        for f in failures:
            print(f"        - {f}")
    print("-" * 82)
    print(f"{passed}/{len(cases)} cases passed (each case is reported on its own; no blended score)\n")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
