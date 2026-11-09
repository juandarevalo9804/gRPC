import tempfile
import unittest
from pathlib import Path

from store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp_dir.name) / "node.sqlite3"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_seed_contains_demo_beds(self):
        beds = self.store.list_beds()
        self.assertEqual(len(beds), 9)
        self.assertTrue(all(bed["status"] == "LIBRE" for bed in beds))

    def test_lock_excludes_a_different_request(self):
        bed_id = self.store.list_beds()[0]["id"]
        self.assertTrue(self.store.try_lock(bed_id, "node-a", "request-a", 5000)[0])
        granted, _ = self.store.try_lock(bed_id, "node-b", "request-b", 5000)
        self.assertFalse(granted)

    def test_confirmation_reserves_and_is_idempotent(self):
        bed_id = self.store.list_beds()[0]["id"]
        self.assertTrue(self.store.try_lock(bed_id, "node-a", "request-a", 5000)[0])
        self.assertTrue(self.store.confirm_reservation(bed_id, "request-a", 1))
        self.assertTrue(self.store.confirm_reservation(bed_id, "request-a", 1))
        bed = self.store.get_bed(bed_id)
        self.assertEqual(bed["status"], "RESERVADA")
        self.assertEqual(bed["version"], 1)

    def test_replica_only_applies_a_newer_version(self):
        bed = self.store.list_beds()[0]
        replica = {
            "id": bed["id"], "hospital": bed["hospital"], "tipo": bed["type"],
            "estado": "RESERVADA", "version": 2,
        }
        self.assertTrue(self.store.apply_replica(replica))
        self.assertFalse(self.store.apply_replica(replica))
        self.assertEqual(self.store.get_bed(bed["id"])["version"], 2)


if __name__ == "__main__":
    unittest.main()