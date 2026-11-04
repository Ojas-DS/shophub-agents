"""Tiny SSE client for the chat API (R6). Prints events as they arrive.

  python scripts/chat_client.py "Refund order 1042" --thread demo
  python scripts/chat_client.py "approve" --thread demo
  python scripts/chat_client.py --interactive --thread demo
"""

import argparse
import json
import sys

import httpx

TOKENS = {"acme": "acme-staff-token", "bolt": "bolt-staff-token", "auditor": "platform-auditor-token"}


def send(base: str, token: str, thread: str, message: str) -> None:
    body = {"message": message, "thread_id": thread}
    with httpx.stream("POST", f"{base}/chat", json=body, timeout=120,
                      headers={"Authorization": f"Bearer {token}"}) as resp:
        if resp.status_code != 200:
            print(f"HTTP {resp.status_code}: {resp.read().decode()}")
            return
        event = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if event == "token":
                    print(data["text"], end="", flush=True)        # tokens as they are produced
                elif event == "final":
                    flag = "  [awaiting your approval]" if data["awaiting_approval"] else ""
                    print(f"\n--- final{flag} ---")
                elif event == "proposal":
                    print(f"\n[proposal] {json.dumps(data['proposal'])}\n{data['text']}")
                else:
                    print(f"[{event}] {json.dumps(data)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("message", nargs="?")
    ap.add_argument("--base", default="http://localhost:8002")
    ap.add_argument("--as", dest="who", default="acme", choices=TOKENS)
    ap.add_argument("--thread", default="demo")
    ap.add_argument("--interactive", "-i", action="store_true")
    args = ap.parse_args()
    token = TOKENS[args.who]
    if args.interactive:
        while True:
            try:
                msg = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if msg:
                send(args.base, token, args.thread, msg)
    elif args.message:
        send(args.base, token, args.thread, args.message)
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
