"""Out-of-band CLI for the queue.

Writes the same files the MCP server reads, so you can add work from a second
terminal while Claude is mid-turn. Nothing here talks to the running session,
which is exactly why it does not steer it.

    q add "fix the failing auth test"
    q list
    q sessions
"""

from __future__ import annotations

import argparse
import sys

from queue_mcp import store

STATUS_MARK = {
    store.PENDING: " ",
    store.IN_PROGRESS: ">",
    store.DONE: "x",
    store.FAILED: "!",
}


def _resolve(session_id: str | None) -> str:
    resolved = session_id or store.last_session()
    if not resolved:
        sys.exit("no queue yet -- add an item with --session <id> first")
    return resolved


def _print_items(result: dict) -> None:
    counts = result["counts"]
    print(f"session {result['session_id']}")
    print(
        f"  {counts.get(store.PENDING, 0)} pending, "
        f"{counts.get(store.IN_PROGRESS, 0)} running, "
        f"{counts.get(store.DONE, 0)} done, "
        f"{counts.get(store.FAILED, 0)} failed"
    )
    if not result["items"]:
        print("  (nothing queued)")
        return
    for item in result["items"]:
        mark = STATUS_MARK.get(item["status"], "?")
        mode = "" if item["mode"] == "fork" else f" [{item['mode']}]"
        print(f"  [{mark}] {item['id']:>3}  {item['text']}{mode}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="q", description="queue-mcp CLI")
    parser.add_argument("-s", "--session", help="session id (defaults to most recent)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="append an item")
    p_add.add_argument("text", nargs="+")
    p_add.add_argument("--inline", action="store_true", help="run in main thread, not a fork")
    p_add.add_argument("--next", action="store_true", help="put it at the front")

    p_list = sub.add_parser("list", help="show the queue")
    p_list.add_argument("-a", "--all", action="store_true", help="include finished items")

    p_rm = sub.add_parser("rm", help="remove an item")
    p_rm.add_argument("item_id", type=int)

    p_mv = sub.add_parser("mv", help="reorder an item")
    p_mv.add_argument("item_id", type=int)
    p_mv.add_argument("position", type=int)

    p_clear = sub.add_parser("clear", help="clear finished items")
    p_clear.add_argument("--all", action="store_true", help="clear everything")

    sub.add_parser("sessions", help="list all known queues")

    args = parser.parse_args(argv)

    if args.cmd == "sessions":
        rows = store.sessions()
        if not rows:
            print("no queues yet")
            return 0
        last = store.last_session()
        for row in rows:
            flag = "*" if row["session_id"] == last else " "
            print(
                f"{flag} {row['session_id']}  "
                f"{row['pending']} pending, {row['in_progress']} running, {row['total']} total"
            )
        return 0

    session = _resolve(args.session)

    if args.cmd == "add":
        item = store.add(
            session,
            " ".join(args.text),
            mode="inline" if args.inline else "fork",
            position=0 if args.next else None,
        )
        print(f"added #{item['id']}: {item['text']}")
    elif args.cmd == "list":
        _print_items(store.listing(session, include_finished=args.all))
    elif args.cmd == "rm":
        item = store.remove(session, args.item_id)
        print(f"removed #{item['id']}: {item['text']}")
    elif args.cmd == "mv":
        item = store.move(session, args.item_id, args.position)
        print(f"moved #{item['id']} to position {args.position}")
    elif args.cmd == "clear":
        print(f"removed {store.clear(session, finished_only=not args.all)} item(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
