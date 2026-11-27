"""Review: a phone's upload lands but Drive stays down, so the phone sends the Apple Health reading again. The person
deletes the reading in Health and the deletion goes in the next segment. A Mac whose listing shows the newer segment
first applies the deletion before the reading, which then must stay deleted."""

import pytest

from byobot.cloud import drive as drive_module
from byobot.cloud.drive import UpstreamError
from fake_drive import FakeDrive
from test_cloud_drive import Mac, setup
from test_cloud_keeper import PHONE

WEIGHT = "HKQuantityTypeIdentifierBodyMass"


def sample(source_id):
    return {"source": "apple_health", "source_id": source_id, "type": WEIGHT,
            "start": "2026-11-23T06:30:00-05:00", "end": "2026-11-23T06:30:00-05:00", "value": 80.0, "unit": "kg"}


def test_a_health_reading_deleted_after_a_lost_upload_stays_deleted(store, tmp_path):
    fake = FakeDrive()
    keeper, relay = setup(fake, tmp_path, store)
    relay.send_samples({"samples": [sample("W1")]})
    fake.fail("POST /upload", "lost")  # the segment lands, its answer is lost, and Drive stays down
    fake.fail("POST /upload", 503, times=drive_module.TRIES - 1)
    with pytest.raises(UpstreamError):
        relay.sync()
    first = [i for i, f in fake.files.items() if f"/segments/{PHONE}/" in f["name"]]
    assert len(first) == 1
    relay.send_samples({"samples": [], "deleted": [{"source": "apple_health", "source_id": "W1", "type": WEIGHT}]})
    relay.sync()
    mac = Mac(fake, tmp_path, store.path)
    fake.lagging = set(first)  # a fresh listing that doesn't show the older segment yet
    mac.run(lambda s, k: k.sync())
    fake.lagging = set()
    for _ in range(3):
        relay.sync()
        mac.run(lambda s, k: k.sync())
    assert "W1" not in {r["payload"]["source_id"] for r in store.records("source_record")}, \
        "a reading deleted in Health came back"
