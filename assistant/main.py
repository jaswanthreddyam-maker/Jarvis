from __future__ import annotations

import argparse
import json

from assistant.app import build_assistant


def run_interactive(show_json: bool) -> None:
    assistant = build_assistant()
    print("Jarvis text control plane ready. Type 'quit' to exit.")
    while True:
        for notification in assistant.poll_notifications():
            print(f"[notification] {notification}")

        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in {"quit", "exit"}:
            break

        response, snapshot = assistant.handle_text(user_input)
        print(response)
        if show_json and snapshot is not None:
            print(json.dumps(snapshot, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis text-first control plane.")
    parser.add_argument("message", nargs="*", help="Optional one-shot command.")
    parser.add_argument("--json", action="store_true", help="Print the task snapshot.")
    args = parser.parse_args()

    if args.message:
        assistant = build_assistant()
        response, snapshot = assistant.handle_text(" ".join(args.message))
        print(response)
        if args.json and snapshot is not None:
            print(json.dumps(snapshot, indent=2))
        return

    run_interactive(show_json=args.json)


if __name__ == "__main__":
    main()
