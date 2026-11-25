"""Temporary: S15 cost breakdown on Windows and macOS runners."""

import sys
import time

import pytest

from .test_scale import history, offline_sync


@pytest.mark.skipif(sys.platform == "linux", reason="diagnostic for Windows and macOS")
def test_diag_s15_io(tmp_path_factory):
    worlds = {n: history("v1", tmp_path_factory.mktemp(f"d{n}"), n) for n in (50, 1000, 2000)}
    configs = {"full+mmap": ("FULL", 268435456), "full": ("FULL", 0), "normal": ("NORMAL", 0)}
    out = {}
    for _ in range(3):
        for name, (sync, mmap) in configs.items():
            for n, w in worlds.items():
                conn = w.replicas["hub"].state.store.conn
                conn.execute(f"PRAGMA synchronous={sync}")
                conn.execute(f"PRAGMA mmap_size={mmap}")
                out.setdefault((name, n), []).append(offline_sync(w))
    for n, w in worlds.items():
        conn = w.replicas["hub"].state.store.conn
        t = time.perf_counter()
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        out[("checkpoint", n)] = [time.perf_counter() - t]
        w.close()
    lines = [f"{k[0]:10} {k[1]:5} " + " ".join(f"{v * 1000:.1f}" for v in vs) for k, vs in out.items()]
    pytest.fail("\n" + "\n".join(lines))
