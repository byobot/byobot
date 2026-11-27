"""The keeper's own tables in the store: cloud_meta, cloud_keys, cloud_read, cloud_refused, cloud_shown, and the
Drive columns of devices. Callers hold the transaction."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

# A device in Drive sync is a row of the store's devices with a class; the hub may know it too.
COLUMNS = "id, class, box, sign, joined_epoch, moved_epoch, last_seq, removed, last_seen_at"
ENROL = ("INSERT INTO devices (id, class, box, sign, joined_epoch, moved_epoch) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT "
         "(id) DO UPDATE SET class = excluded.class, box = excluded.box, sign = excluded.sign, joined_epoch = "
         "excluded.joined_epoch, moved_epoch = excluded.moved_epoch, last_seq = NULL, removed = 0")
TABLES = ("cloud_meta", "cloud_keys", "cloud_read", "cloud_shown", "cloud_refused")


class State:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- cloud_meta ------------------------------------------------------------------

    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM cloud_meta WHERE key = ?", (key,)).fetchone()
        return row and row[0]

    def meta(self, keys: Iterable[str]) -> dict[str, str]:
        keys = list(keys)
        return dict(self.conn.execute(f"SELECT key, value FROM cloud_meta WHERE key IN ({', '.join('?' * len(keys))})",
                                      keys))

    def set(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT INTO cloud_meta VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = "
                          "excluded.value", (key, str(value)))

    def add(self, pairs: Iterable[tuple[str, Any]]) -> None:
        self.conn.executemany("INSERT INTO cloud_meta VALUES (?, ?)", [(k, str(v)) for k, v in pairs])

    def unset(self, *keys: str, prefixes: Iterable[str] = ()) -> None:
        for key in keys:
            self.conn.execute("DELETE FROM cloud_meta WHERE key = ?", (key,))
        for prefix in prefixes:
            self.conn.execute("DELETE FROM cloud_meta WHERE key LIKE ?", (f"{prefix}%",))

    # -- cloud_keys ------------------------------------------------------------------

    def key(self, epoch: int) -> str | None:
        row = self.conn.execute("SELECT key FROM cloud_keys WHERE epoch = ?", (epoch,)).fetchone()
        return row and row[0]

    def add_keys(self, keys: Iterable[tuple[int, str]]) -> None:
        self.conn.executemany("INSERT OR IGNORE INTO cloud_keys VALUES (?, ?)", list(keys))

    def key_epochs_before(self, epoch: int) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT epoch FROM cloud_keys WHERE epoch < ?", (epoch,))]

    def drop_keys(self, before: int | None = None, after: int | None = None) -> None:
        if before is not None:
            self.conn.execute("DELETE FROM cloud_keys WHERE epoch < ?", (before,))
        if after is not None:
            self.conn.execute("DELETE FROM cloud_keys WHERE epoch > ?", (after,))

    # -- devices ---------------------------------------------------------------------

    def devices(self, removed: bool = False) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            f"SELECT {COLUMNS} FROM devices WHERE class IS NOT NULL AND removed = ? "
            "ORDER BY class != 'keeper', joined_epoch, id", (int(removed),))]

    def device(self, device_id: str) -> dict | None:
        row = self.conn.execute(f"SELECT {COLUMNS} FROM devices WHERE id = ? AND class IS NOT NULL",
                                (device_id,)).fetchone()
        return row and dict(row)

    def enrol(self, rows: Iterable[tuple]) -> None:
        """Each row: id, class, box, sign, joined_epoch, moved_epoch."""
        self.conn.executemany(ENROL, list(rows))

    def rejoin(self, device_id: str, box: str, sign: str, epoch: int) -> None:
        self.conn.execute("UPDATE devices SET box = ?, sign = ?, joined_epoch = ?, moved_epoch = ?, last_seq = NULL, "
                          "removed = 0 WHERE id = ?", (box, sign, epoch, epoch, device_id))

    def set_device_keys(self, device_id: str, box: str, sign: str) -> None:
        self.conn.execute("UPDATE devices SET box = ?, sign = ? WHERE id = ?", (box, sign, device_id))

    def moved(self, device_id: str, epoch: int, seq: int) -> None:
        self.conn.execute("UPDATE devices SET moved_epoch = max(moved_epoch, ?), last_seq = max(coalesce(last_seq, 0), "
                          "?) WHERE id = ?", (epoch, seq, device_id))

    def seen(self, device_id: str, at: str) -> None:
        self.conn.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (at, device_id))

    def remove(self, device_id: str) -> None:
        self.conn.execute("UPDATE devices SET removed = 1 WHERE id = ?", (device_id,))

    def leave(self, relays_only: bool = False) -> None:
        """Take devices out of Drive sync; one the hub doesn't know goes altogether."""
        where = "class = 'relay'" if relays_only else "class IS NOT NULL"
        self.conn.execute("UPDATE devices SET class = NULL, box = NULL, sign = NULL, joined_epoch = NULL, moved_epoch = "
                          f"NULL, last_seq = NULL, removed = 0 WHERE {where}")
        self.conn.execute("DELETE FROM devices WHERE class IS NULL AND token_sha256 IS NULL")

    # -- cloud_read, cloud_refused, cloud_shown ------------------------------------------

    def checksums(self) -> dict[str, str]:
        return dict(self.conn.execute("SELECT path, checksum FROM cloud_read"))

    def outcomes(self, prefix: str = "") -> dict[str, str]:
        return dict(self.conn.execute("SELECT path, outcome FROM cloud_read WHERE path LIKE ? ORDER BY path",
                                      (f"{prefix}%",)))

    def mark(self, path: str, checksum: str, outcome: str) -> None:
        self.conn.execute("INSERT INTO cloud_read VALUES (?, ?, ?) ON CONFLICT (path) DO UPDATE SET checksum = "
                          "excluded.checksum, outcome = excluded.outcome", (path, checksum, outcome))

    def any_outcome(self, containing: str) -> bool:
        return self.conn.execute("SELECT 1 FROM cloud_read WHERE instr(outcome, ?) LIMIT 1",
                                 (containing,)).fetchone() is not None

    def forget_outcomes(self, containing: str) -> None:
        self.conn.execute("DELETE FROM cloud_read WHERE instr(outcome, ?)", (containing,))

    def forget(self, paths: Iterable[str] = (), prefix: str | None = None) -> None:
        """What was read and refused from these files, so they're read again or not at all."""
        rows = [(p,) for p in paths]
        for table in ("cloud_read", "cloud_refused"):
            self.conn.executemany(f"DELETE FROM {table} WHERE path = ?", rows)
            if prefix is not None:
                self.conn.execute(f"DELETE FROM {table} WHERE path LIKE ?", (f"{prefix}%",))

    def refuse(self, rows: Iterable[tuple[str, int, str]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO cloud_refused VALUES (?, ?, ?)", list(rows))

    def refused(self) -> list[tuple[str, int, str]]:
        return [tuple(r) for r in self.conn.execute("SELECT path, line, reason FROM cloud_refused ORDER BY path, line")]

    def shown(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT event_id FROM cloud_shown")}

    def show(self, ids: Iterable[str]) -> None:
        self.conn.executemany("INSERT OR IGNORE INTO cloud_shown VALUES (?)", [(i,) for i in ids])

    def clear(self, *tables: str) -> None:
        for table in tables:
            assert table in TABLES
            self.conn.execute(f"DELETE FROM {table}")
