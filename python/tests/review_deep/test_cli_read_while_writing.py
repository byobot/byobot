"""Deep review (cli): a read answers while another process writes.

Proves round-1 suspicion "Every request opens a Store, whose _migrate runs BEGIN IMMEDIATE" for the CLI and MCP:
opening a Store always takes SQLite's write lock (and writes meta), so `byobot list` or an agent's first tool call
waits for any write in another process and, past the 5 s busy timeout, dies with `database is locked`. In WAL mode
a reader never has to wait for a writer.
"""

import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _cli_helpers import via_cli, via_mcp  # noqa: E402

HOLD_S = 6.5  # longer than the store's 5 s busy timeout, as a big ingest, restore or rebuild is


def test_cli_a_read_answers_while_another_process_holds_the_write_lock(byobot_home):
    """CLI-13."""
    assert via_cli(["init"])[0] == "ok"
    ready = threading.Event()

    def writer():
        conn = sqlite3.connect(byobot_home / "byobot.db", isolation_level=None, timeout=10)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO meta VALUES ('example_long_write', '1') ON CONFLICT (key) DO NOTHING")
        ready.set()
        time.sleep(HOLD_S)
        conn.execute("COMMIT")
        conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    ready.wait(5)
    try:
        cli_answer = via_cli(["list", "session"])
        mcp_answer = via_mcp("list", {"kind": "session"})
    finally:
        thread.join()
    assert (cli_answer[0], mcp_answer[0]) == ("ok", "ok"), (cli_answer, mcp_answer)
