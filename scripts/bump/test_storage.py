"""Long-lived storage contracts: mixed references, crash recovery and isolation."""

import copy
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import fixtures
import pipeline
import publisher
import storage
import sync

ROOT = Path(__file__).resolve().parents[2]


class MemoryStore:
    """In-memory object boundary with explicit interrupted-operation injection."""

    def __init__(self):
        self.objects = {}
        self.fail = None
        self.deleted = []

    def get(self, role, key):
        return self.objects.get((role, key))

    def put(self, role, key, body, content_type):
        if self.fail == ("put", role, key):
            self.fail = None
            raise OSError("injected upload failure")
        self.objects[role, key] = (body, content_type)

    def delete(self, role, key):
        if self.fail == ("delete", role, key):
            self.fail = None
            raise OSError("injected delete failure")
        self.deleted.append((role, key))
        self.objects.pop((role, key), None)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MemoryStore()

    def stage(self, label, ids, branch=None):
        directory = self.root / label
        directory.mkdir(exist_ok=True)
        items = []
        for digest in ids:
            for ext in storage.TYPES:
                (directory / f"{digest}.{ext}").write_bytes(f"{digest}.{ext}".encode())
            items.append(
                {"id": digest, "url": f"https://byrdocs.org/files/{digest}.pdf"}
            )
        manifest = {"files": [{"md5": digest} for digest in ids]}
        storage.stage(self.store, label, manifest, items, directory, branch)

    def publisher(self, items):
        expected = {
            "metadata.json": (json.dumps(items).encode(), "application/json"),
            "sitemap.xml": (b"<urlset />", "application/xml"),
        }
        for key, (body, kind) in expected.items():
            self.store.put("data", key, body, kind)
        return expected

    def test_replace_preserves_ordinary_shared_open_and_unknown_objects(self):
        old, shared, new, ordinary, waiting = [letter * 32 for letter in "abcde"]
        self.stage("fixtures", [ordinary])
        self.stage("A", [old, shared], "A")
        storage.publish(self.store, "A", self.publisher)
        self.stage("pending", [waiting], "pending")
        self.stage("B", [new], "B")
        storage.publish(self.store, "B", self.publisher)
        unknown = ("files", "f" * 32 + ".pdf")
        self.store.objects[unknown] = (b"not ours", "application/pdf")
        storage.cleanup(self.store, {shared}, {"pending"}, {"A"})
        self.assertIsNone(self.store.get("files", old + ".pdf"))
        for digest in (shared, new, ordinary, waiting):
            for obj in storage.object_names([digest]):
                role, key, _ = storage.split_object(obj)
                self.assertIsNotNone(self.store.get(role, key))
        self.assertIn(unknown, self.store.objects)

    def test_upload_crash_journal_allows_retry_without_adopting_existing_objects(self):
        digest = "a" * 32
        existing = ("files", digest + ".jpg")
        self.store.objects[existing] = ((digest + ".jpg").encode(), "image/jpeg")
        self.store.fail = ("put", "backup", digest + ".pdf")
        with self.assertRaises(OSError):
            self.stage("A", [digest])
        self.assertFalse(storage.load_state(self.store)["batches"]["A"]["ready"])
        self.stage("A", [digest])
        state = storage.load_state(self.store)
        self.assertTrue(state["batches"]["A"]["ready"])
        self.assertNotIn("files/" + digest + ".jpg", state["owned"])
        self.store.objects["files", digest + ".pdf"] = (b"corrupt", "application/pdf")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.stage("A", [digest])

    def test_partial_publication_blocks_cleanup_and_retry_confirms_both_roots(self):
        self.stage("A", ["a" * 32], "A")
        storage.publish(self.store, "A", self.publisher)
        self.stage("B", ["b" * 32], "B")
        self.store.fail = ("put", "data", "sitemap.xml")
        with self.assertRaises(OSError):
            storage.publish(self.store, "B", self.publisher)
        self.assertEqual(storage.load_state(self.store)["published"], "A")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            storage.cleanup(self.store, set(), set(), {"A"})
        storage.publish(self.store, "B", self.publisher)
        storage.cleanup(self.store, set(), set(), {"A"})
        self.assertIsNone(self.store.get("files", "a" * 32 + ".pdf"))
        snapshot = copy.deepcopy(self.store.objects)
        storage.publish(
            self.store, "B", lambda _: self.fail("repeat must not republish")
        )
        self.assertEqual(snapshot, self.store.objects)

    def test_delete_failure_retries_remaining_and_rechecks_new_references(self):
        a, b = "a" * 32, "b" * 32
        self.stage("A", [a], "A")
        self.stage("B", [b], "B")
        storage.publish(self.store, "B", self.publisher)
        self.store.fail = ("delete", "files", a + ".jpg")
        with self.assertRaises(OSError):
            storage.cleanup(self.store, set(), set(), {"A"})
        state = storage.load_state(self.store)
        self.assertTrue(state["pending_delete"])
        self.assertNotIn("backup/" + a + ".pdf", state["pending_delete"])
        deleted = list(self.store.deleted)
        storage.cleanup(self.store, {a}, set(), {"A"})
        self.assertEqual(deleted, self.store.deleted)
        storage.cleanup(self.store, set(), set(), {"A"})
        self.assertIsNone(self.store.get("files", a + ".pdf"))
        self.assertEqual(self.store.deleted.count(("backup", a + ".pdf")), 1)

    def test_confirmed_objects_survive_final_state_write_failure(self):
        self.stage("A", ["a" * 32], "A")
        storage.publish(self.store, "A", self.publisher)
        self.stage("B", ["b" * 32], "B")
        original_put = self.store.put

        def fail_final_state(role, key, body, kind):
            if key == storage.STATE_KEY:
                state = json.loads(body)
                if state["published"] == "B" and state["publishing"] is None:
                    raise OSError("final state write interrupted")
            original_put(role, key, body, kind)

        with (
            patch.object(self.store, "put", side_effect=fail_final_state),
            self.assertRaises(OSError),
        ):
            storage.publish(self.store, "B", self.publisher)
        self.assertEqual(storage.load_state(self.store)["publishing"], "B")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            storage.cleanup(self.store, set(), set(), {"A"})
        storage.publish(self.store, "B", self.publisher)
        storage.cleanup(self.store, set(), set(), {"A"})
        self.assertIsNone(self.store.get("files", "a" * 32 + ".pdf"))

    def test_orphans_expire_and_closed_pr_uploads_are_reclaimed(self):
        self.stage("orphan", ["a" * 32], "no-pr")
        self.stage("closed", ["b" * 32], "closed")
        storage.cleanup(self.store, set(), set(), {"closed"})
        self.assertIsNone(self.store.get("files", "b" * 32 + ".pdf"))
        self.assertIsNotNone(self.store.get("files", "a" * 32 + ".pdf"))
        storage.cleanup(
            self.store,
            set(),
            set(),
            set(),
            datetime.now(UTC) + timedelta(days=2),
        )
        self.assertFalse(storage.load_state(self.store)["owned"])

    def test_modified_publication_or_invalid_cleanup_key_fails_closed(self):
        self.stage("A", ["a" * 32])
        storage.publish(self.store, "A", self.publisher)
        self.store.objects["data", "metadata.json"] = (b"[]", "application/json")
        with self.assertRaisesRegex(ValueError, "changed"):
            storage.cleanup(self.store, set(), set(), set())
        state = storage.load_state(self.store)
        state["owned"].append("data/metadata.json")
        self.store.put(
            "data", storage.STATE_KEY, json.dumps(state).encode(), "application/json"
        )
        with self.assertRaisesRegex(ValueError, "Invalid managed"):
            storage.load_state(self.store)

    def test_account_bucket_and_repository_configuration_are_restricted(self):
        env = {
            "GITHUB_REPOSITORY": sync.FORK,
            "BUMP_TEST_STORAGE_ENABLED": "true",
            "BUMP_TEST_ACCOUNT_ID": "a" * 32,
            "BUMP_TEST_ACCESS_KEY_ID": "test",
            "BUMP_TEST_SECRET_ACCESS_KEY": "test",
            "BUMP_TEST_BACKUP_REGION": "auto",
            "BUMP_TEST_BACKUP_ENDPOINT": "https://"
            + "a" * 32
            + ".r2.cloudflarestorage.com",
        }
        env.update(
            {
                f"BUMP_TEST_{role.upper()}_BUCKET": name
                for role, name in storage.BUCKETS.items()
            }
        )
        storage.settings(env)
        for field, value in [
            ("GITHUB_REPOSITORY", "byrdocs/byrdocs-archive"),
            ("BUMP_TEST_FILES_BUCKET", "byrdocs-file"),
            ("BUMP_TEST_STORAGE_ENABLED", "false"),
            ("BUMP_TEST_BACKUP_ENDPOINT", "https://production.example"),
        ]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                storage.settings({**env, field: value})

    def test_fixtures_are_deterministic_and_use_existing_three_schemas(self):
        manifest, items = fixtures.generate(ROOT, self.root / "one")
        repeated, _ = fixtures.generate(ROOT, self.root / "two")
        self.assertEqual(manifest, repeated)
        self.assertEqual({item["type"] for item in items}, {"book", "doc", "test"})
        for path in (self.root / "one/files").iterdir():
            self.assertEqual(
                path.read_bytes(), (self.root / "two/files" / path.name).read_bytes()
            )
            if path.suffix == ".pdf":
                self.assertIn(
                    "Self-authored fixture",
                    sync.PdfReader(path).pages[0].extract_text(),
                )


class PublisherBoundaryTests(unittest.TestCase):
    def test_superseded_default_branch_stops_publication_and_cleanup(self):
        with (
            patch.object(pipeline.subprocess, "check_output", return_value="old\n"),
            patch.object(sync, "gh", return_value='{"object":{"sha":"new"}}'),
            self.assertRaisesRegex(ValueError, "Default branch changed"),
        ):
            pipeline.require_current_default(ROOT)

    def test_workflows_keep_cloud_credentials_out_of_pr_jobs(self):
        workflow = sync.YAML_CODEC.load(
            (ROOT / ".github/workflows/bump-sync.yml").read_text(encoding="utf-8")
        )
        guard = workflow["jobs"]["storage"]["if"]
        self.assertIn("github.repository == 'Shxiao111/byrdocs-archive'", guard)
        self.assertNotIn("pull_request", guard)
        self.assertNotIn("secrets", str(workflow["jobs"]["test"]))
        cloud = sync.YAML_CODEC.load(
            (ROOT / ".github/workflows/bump-storage.yml").read_text(encoding="utf-8")
        )
        job = cloud["jobs"]["storage"]
        self.assertIn("github.repository == 'Shxiao111/byrdocs-archive'", job["if"])
        self.assertEqual(
            job["concurrency"]["group"],
            workflow["jobs"]["sync"]["concurrency"]["group"],
        )
        self.assertTrue(
            all(
                "secrets.BUMP_TEST_" in value
                for value in job["env"].values()
                if "secrets." in value
            )
        )

    def test_local_api_supports_empty_publish_and_rejects_real_mutations(self):
        with publisher.local_api(port=0) as server:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(base + "/api/file/notPublished") as response:
                self.assertEqual(json.load(response), {"success": True, "files": []})
            request = urllib.request.Request(
                base + "/api/file/publish",
                data=b'{"ids":[]}',
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request) as response:
                self.assertTrue(json.load(response)["success"])
        with (
            self.assertRaisesRegex(ValueError, "Unexpected publisher"),
            publisher.local_api(port=0) as server,
        ):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/file/publish",
                data=b'{"ids":[42]}',
            )
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(request)


if __name__ == "__main__":
    unittest.main()
