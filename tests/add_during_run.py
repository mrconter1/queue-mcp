"""Can work be added to a queue while a queue is being worked?

That is the whole promise of this thing: you think of the next task while the
agent is mid-turn, you put it on the board, and it is there when the agent asks
for the next item. This checks the promise end to end rather than by reasoning
about the code - two writers at once on one file is exactly where reasoning goes
wrong.

Run it from the repository root::

    python tests/add_during_run.py

It builds its own queue directory under the system temp, starts its own server
on a spare port, and deletes both when it is done, so it never touches the
queues on the machine it runs on. The browser checks need Playwright; without it
they are skipped and the rest still runs.

Exit code is 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("QUEUE_MCP_TEST_PORT", "8791"))
BASE = f"http://127.0.0.1:{PORT}"

# A queue directory of its own, so a machine's real queues are untouchable from
# here. store reads this at import time, which is why it is set before the
# import below. An inherited one is kept rather than replaced: the hammer below
# runs this same file as child processes, and a child that made its own
# directory would be writing to a queue nobody was watching.
_INHERITED = os.environ.get("QUEUE_MCP_HOME")
HOME = Path(_INHERITED) if _INHERITED else Path(tempfile.mkdtemp(prefix="queue-mcp-test-"))
OWNS_HOME = _INHERITED is None
os.environ["QUEUE_MCP_HOME"] = str(HOME)

sys.path.insert(0, str(REPO))
from queue_mcp import store  # noqa: E402  (after QUEUE_MCP_HOME is set)

PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (passed if ok else failed).append(name)
    print(f"  [{'pass' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")
    return ok


def post(path: str, body: dict) -> tuple[int, dict]:
    """A write exactly as the board makes it, JSON content type and all."""
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def start_server() -> subprocess.Popen:
    process = subprocess.Popen(
        [str(PYTHON), "-m", "queue_mcp.server"],
        cwd=str(REPO),
        env={**os.environ, "QUEUE_MCP_PORT": str(PORT), "QUEUE_MCP_HOME": str(HOME)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(80):
        time.sleep(0.25)
        try:
            with urllib.request.urlopen(f"{BASE}/api/state", timeout=2):
                return process
        except Exception:
            continue
    raise SystemExit(f"the test server never came up on {BASE}")


def stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


# --- 1. an add during a run lands last, shows up, and is claimed next ----------

def added_work_is_picked_up() -> None:
    print("1. an item added while another is running")
    session = "during-run"
    store.add(session, "already queued", "inline")
    running = store.claim_next(session)

    status, state = post("/api/add", {"session": session, "text": "added while it ran", "mode": "inline"})
    check("the board's add is accepted", status == 200, f"status {status}")

    pending = [i for i in state.get("items", []) if i["status"] == "pending"]
    check("it is in the answer the board redraws from",
          any(i["text"] == "added while it ran" for i in pending))
    check("it is last in the pending order",
          bool(pending) and pending[-1]["text"] == "added while it ran",
          f"order {[i['text'] for i in pending]}")
    check("the running item is untouched",
          store.current(session)["id"] == running["id"])

    store.finish(session, running["id"], "done")
    claimed = store.claim_next(session)
    check("and it is what the next claim hands over",
          claimed["text"] == "added while it ran", f"got {claimed['text']!r}")
    store.clear(session, finished_only=False)


# --- 2. the mode the board asked for is the mode it gets ----------------------

def the_mode_survives() -> None:
    print("2. the mode the board chose")
    session = "mode-kept"
    for mode in ("inline", "fork"):
        status, state = post("/api/add", {"session": session, "text": f"a {mode} task", "mode": mode})
        item = [i for i in state["items"] if i["text"] == f"a {mode} task"][0]
        check(f"an item added as {mode} is stored as {mode}",
              status == 200 and item["mode"] == mode, f"stored {item['mode']!r}")
    status, _ = post("/api/add", {"session": session, "text": "nonsense mode", "mode": "sideways"})
    check("an impossible mode is refused, not silently changed", status == 400, f"status {status}")
    store.clear(session, finished_only=False)


# --- 3. two writers at once -------------------------------------------------

def worker(session: str, tag: str, count: int) -> None:
    """One process adding as fast as it can, used by the hammer below."""
    for n in range(count):
        for attempt in range(5):
            try:
                store.add(session, f"{tag}-{n}", "inline")
                break
            except TimeoutError:
                time.sleep(0.2 * (attempt + 1))
        else:
            print(f"worker {tag} gave up on {n}", file=sys.stderr)


def nothing_is_lost_under_load() -> None:
    print("3. several writers at once")
    session = "hammer"
    writers, each = 4, 25
    children = [
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", session, f"w{w}", str(each)],
            cwd=str(REPO),
            env={**os.environ, "QUEUE_MCP_HOME": str(HOME)},
        )
        for w in range(writers)
    ]

    # While they write, the session does what a session does: claim, finish,
    # claim. This is the collision the lock exists for.
    claims = 0
    while any(child.poll() is None for child in children):
        item = store.claim_next(session)
        if item:
            claims += 1
            store.finish(session, item["id"], "ok")
        time.sleep(0.01)
    for child in children:
        child.wait()

    # Anything still pending, plus everything that finished, must add up.
    raw = (HOME / "sessions" / f"{session}.json").read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        parsed = True
    except json.JSONDecodeError:
        data, parsed = {"items": []}, False
    check("the file is still valid JSON", parsed)

    # A finished item stays in the file as well as going to history, so the
    # file is the population and history is a copy of part of it. Only a
    # dropped item leaves the file, and only those are counted from history.
    items = data.get("items", [])
    dropped = [r for r in store.history(session_id=session, limit=10000)["records"]
               if r.get("outcome") == "dropped"]
    ids = [i["id"] for i in items] + [r["id"] for r in dropped]
    expected = writers * each
    check(f"all {expected} added items are accounted for",
          len(ids) == expected, f"{len(ids)} found ({len(items)} queued, {len(dropped)} dropped)")
    check("no id was handed out twice", len(set(ids)) == len(ids),
          f"{len(ids) - len(set(ids))} duplicates")

    texts = {i["text"] for i in items} | {r["text"] for r in dropped}
    wanted = {f"w{w}-{n}" for w in range(writers) for n in range(each)}
    check("every task that was added is somewhere", texts == wanted,
          f"missing {sorted(wanted - texts)[:5]}")
    print(f"     ({claims} claimed and finished while {expected} were being added)")
    store.clear(session, finished_only=False)


# --- 4. a crashed writer must not wedge the queue ---------------------------

def a_dead_lock_expires() -> None:
    print("4. a writer that died holding the lock")
    session = "wedged"
    store.add(session, "before the crash", "inline")
    lock = (HOME / "sessions" / f"{session}.lock")
    lock.write_text("", encoding="utf-8")

    started = time.time()
    try:
        store.add(session, "after the crash", "inline")
        took = time.time() - started
        check("the queue recovers by itself", True, f"waited {took:.0f}s for the stale lock")
        check("it waits rather than trampling a live lock", took >= store.STALE_LOCK_SECONDS - 1,
              f"{took:.1f}s")
    except TimeoutError as exc:
        check("the queue recovers by itself", False, str(exc))
    finally:
        lock.unlink(missing_ok=True)
    store.clear(session, finished_only=False)


# --- 5. and 6. the board itself ---------------------------------------------

def the_board_keeps_your_typing(server: subprocess.Popen) -> subprocess.Popen:
    print("5. the board while it is being used")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("     (skipped: Playwright is not installed for this interpreter)")
        return server

    session = "board-use"
    store.add(session, "one", "inline")
    store.add(session, "two", "inline")
    store.add(session, "three", "inline")
    running = store.claim_next(session)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        problems: list[str] = []
        page.on("pageerror", lambda e: problems.append(str(e)))
        # load, never networkidle: the event stream never goes quiet.
        page.goto(f"{BASE}/?session={session}", wait_until="load")
        page.wait_for_timeout(900)

        rows = lambda: page.eval_on_selector_all(
            "#list li .text", "els => els.map(e => e.textContent.trim())")

        # typing in one row while an item arrives from elsewhere
        page.click("#list li .text >> nth=1")
        page.wait_for_timeout(250)
        page.fill("#edit-text", "two, being typed right now")
        store.add(session, "arrived mid-edit", "inline")
        page.wait_for_timeout(1400)
        kept = page.input_value("#edit-text")
        check("typing survives an item arriving", kept == "two, being typed right now", repr(kept))
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        check("and the arrival shows up once the edit is done",
              any("arrived mid-edit" in r for r in rows()), str(rows()))

        # an item arriving in the middle of a drag: the drop must still do what
        # the pointer said, and the arrival must not be lost either
        order_before = rows()
        # Pending rows are the draggable ones; the running row and the
        # "N finished" expander are not, so they are picked out by that flag.
        draggable = page.query_selector_all("#list li[data-pending]")
        check("there are pending rows to drag", len(draggable) >= 2, f"{len(draggable)} found")
        source, target = (draggable[-1], draggable[0]) if len(draggable) >= 2 else (None, None)
        if source and target:
            sbox, tbox = source.bounding_box(), target.bounding_box()
            page.mouse.move(sbox["x"] + 30, sbox["y"] + sbox["height"] / 2)
            page.mouse.down()
            page.mouse.move(tbox["x"] + 30, tbox["y"] + 4, steps=8)
            store.add(session, "arrived mid-drag", "inline")
            page.wait_for_timeout(900)
            page.mouse.up()
            page.wait_for_timeout(900)
            after = rows()
            check("a drag still applies when an item arrives during it",
                  after != order_before, f"{order_before} -> {after}")
            check("and the item that arrived mid-drag is not lost",
                  any("arrived mid-drag" in r for r in after), str(after))

        # adding from the page itself, while an item is in progress
        page.fill("#add-text", "typed into the board")
        page.click("#add button[type=submit]")
        page.wait_for_timeout(800)
        check("the board can add while a task is running",
              any("typed into the board" in r for r in rows()), str(rows()))
        check("the running item is still the running item",
              store.current(session)["id"] == running["id"])

        # an add while the server is being restarted: never silently lost
        stop_server(server)
        page.fill("#add-text", "posted at a dead server")
        page.click("#add button[type=submit]")
        page.wait_for_timeout(2500)
        complained = page.inner_text("#error").strip()
        stored = any(i["text"] == "posted at a dead server" for i in store.listing(session)["items"])
        check("an add to a dead server says so instead of vanishing",
              bool(complained) or stored,
              f"error line {complained!r}, stored {stored}")
        typed = page.input_value("#add-text")
        check("and what was typed is still in the box to try again",
              typed == "posted at a dead server" or stored, repr(typed))

        server = start_server()
        page.wait_for_timeout(3000)
        store.add(session, "after the restart", "inline")
        page.wait_for_timeout(2500)
        check("the page picks up again after the restart",
              any("after the restart" in r for r in rows()), str(rows()))
        check("no page errors throughout", not problems, str(problems))
        browser.close()

    store.clear(session, finished_only=False)
    return server


def main() -> int:
    print(f"queue home: {HOME}")
    print(f"server:     {BASE}\n")
    server = start_server()
    try:
        added_work_is_picked_up()
        the_mode_survives()
        nothing_is_lost_under_load()
        a_dead_lock_expires()
        server = the_board_keeps_your_typing(server)
    finally:
        stop_server(server)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    for name in failed:
        print(f"  failed: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2], sys.argv[3], int(sys.argv[4]))
        raise SystemExit(0)
    code = main()
    if OWNS_HOME:
        shutil.rmtree(HOME, ignore_errors=True)
    raise SystemExit(code)
