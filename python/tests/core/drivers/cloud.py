"""Schema 1 with every delivery carried through cloud segments.

Each delivery is cut into encrypted, signed segments in a folder, one copy is duplicated and one is cut short, and
the files are read in a shuffled order. The broken copy must be refused and the rest must land as if delivered
directly. Outcomes are mapped back to the order the events were sent in.
"""

from __future__ import annotations

import json
import random

from byobot.cloud import segment
from byobot.cloud.crypto import CloudError, Identity, new_key
from byobot.cloud.folder import MemoryFolder
from byobot.cloud.keeper import apply

from .v1 import V1, Replica

EPOCH = 1
PER_SEGMENT = 5


class Cloud(V1):
    name = "cloud"

    def __init__(self):
        super().__init__()
        self.key = new_key()
        self.writer = Identity.generate()
        self.seq = 0
        self.rng = random.Random(0)

    def deliver(self, rep: Replica, events: list[dict]) -> list[tuple[str, str]]:
        if not events:
            return []
        store_id = rep.store.store_id
        folder, parts = MemoryFolder(), {}
        for start in range(0, len(events), PER_SEGMENT):
            self.seq += 1
            path = segment.name("segment", EPOCH, self.seq, "peer")
            batch = events[start:start + PER_SEGMENT]
            folder.create(path, segment.write("segment", store_id, "peer", EPOCH, self.seq,
                                              [{"events": {"events": batch}}], self.key, self.writer))
            parts[path] = range(start, start + len(batch))
        paths = list(parts)
        broken = paths[self.rng.randrange(len(paths))]
        self.seq += 1
        cut = segment.name("segment", EPOCH, self.seq, "peer")
        whole = segment.write("segment", store_id, "peer", EPOCH, self.seq,
                              [{"events": {"events": [events[i] for i in parts[broken]]}}], self.key, self.writer)
        folder.files[cut] = whole[:self.rng.randrange(1, len(whole))]
        reads = paths + [self.rng.choice(paths), cut]
        self.rng.shuffle(reads)
        out: dict[int, tuple[str, str]] = {}
        for path in reads:
            try:
                _, messages = segment.read(folder.read(path), path=path, store_id=store_id, key=self.key,
                                           sign_public=self.writer.public()["sign"])
            except CloudError:
                assert path == cut, f"a good segment was refused: {path}"
                continue
            assert path != cut, "a segment cut short was read"
            for i, got in zip(parts[path], apply(rep.store, "peer", "keeper", messages)):
                out.setdefault(i, got)
        assert sorted(out) == list(range(len(events))), json.dumps(sorted(set(range(len(events))) - set(out)))
        return [out[i] for i in range(len(events))]
