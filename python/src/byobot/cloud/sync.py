"""Syncing with Drive: one sync at a time per store, and the poller `serve`, `remote` and `mcp` run."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from byobot.core.store import Store
from byobot.util import ByobotError


EVERY_S = 60  # how often `serve`, `remote` and `mcp` sync with Drive


def drive_folder(store: Store, quick: bool = False) -> Any:
    from byobot.cloud.drive import Drive, DriveFolder

    return DriveFolder(store.store_id, Drive(tries=2, timeout=10) if quick else Drive())


_folders: dict[str, Any] = {}


def lock_path(store: Store) -> Path:
    """Beside the database, not named for its store id, which `cloud recover` changes."""
    return Path(f"{store.path}.cloud-lock")


@contextmanager
def lock(store: Store, wait: bool = True) -> Iterator[bool]:
    from byobot.cloud.lock import held

    with held(lock_path(store), wait) as mine:
        yield mine


def autosync(store: Store, folder: Any = None, every: float = EVERY_S) -> dict | None:
    """Sync with Drive when it's due, for the poller in `serve`, `remote` and `mcp`. Never waits on another process's
    sync and never raises: serving goes on with what the store holds, and `cloud status` shows the error."""
    from byobot.cloud.keeper import Keeper, due, tried

    if not due(store, every):
        return None
    with lock(store, wait=False) as mine:
        if not mine or not due(store, every):
            return None
        tried(store)
        keeper = Keeper(store, folder)
        try:
            if keeper.folder is None:  # one folder per store for the process, so the change feed carries on
                keeper.folder = _folders.get(store.store_id) or drive_folder(store, quick=True)
                _folders[store.store_id] = keeper.folder
            return keeper.sync()
        except Exception as exc:  # noqa: BLE001 - Drive failing never fails a read
            _folders.pop(store.store_id, None)
            message = exc.message if isinstance(exc, ByobotError) else f"{type(exc).__name__}: {exc}"
            keeper.failed(message)
            return {"error": message}


class CloudPoller(threading.Thread):
    """While `serve`, `remote` or `mcp` runs, sync with Drive once a minute if Drive sync is on for this store. Reads
    never sync themselves."""

    def __init__(self, db: Path, log: Callable[[str], None], every: float = EVERY_S):
        super().__init__(name="byobot-cloud", daemon=True)
        self.db, self.log, self.every = db, log, every
        self.stopped = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        while True:
            self.poll()
            if self.stopped.wait(self.every):
                return

    def poll(self) -> dict | None:
        if not self.db.exists():
            return None
        try:
            with Store(self.db, migrate=False) as store:
                report = autosync(store, every=self.every / 2)  # another process synced in the last half minute
        except Exception as exc:  # noqa: BLE001 - serving goes on
            report = {"error": f"{type(exc).__name__}: {exc}"}
        if report is not None:
            self._note(report)
        return report

    def _note(self, report: dict) -> None:
        error = report.get("error")
        if error and error != self.error:
            self.log(f"Drive sync: {error}")
        elif not error and self.error:
            self.log("Drive sync: working again")
        self.error = error
        if report.get("join") == "admitted":
            self.log("Drive sync: a phone joined")
        if report.get("read"):
            self.log(f"Drive sync: read {report['read']} file{'s' if report['read'] != 1 else ''} from phones")
        if "new_epoch" in report:
            self.log(f"Drive sync: new epoch ({report['new_epoch']['reason']})")
