"""Disposable real-R2 recovery exercise, restricted to an uninitialized test setup."""

import json
import shutil
from datetime import UTC, datetime, timedelta

import fixtures
import publisher
import storage
import sync


class FailOnce:
    """Inject one client failure while all other operations use the actual test R2."""

    def __init__(self, store, operation, role, key):
        self.store = store
        self.target = (operation, role, key)
        self.config = store.config

    def get(self, role, key):
        return self.store.get(role, key)

    def check(self, operation, role, key):
        if self.target == (operation, role, key):
            self.target = None
            raise OSError("Expected exercise failure")

    def put(self, role, key, body, kind):
        self.check("put", role, key)
        self.store.put(role, key, body, kind)

    def delete(self, role, key):
        self.check("delete", role, key)
        self.store.delete(role, key)


def expect_failure(action):
    try:
        action()
    except OSError as error:
        if str(error) != "Expected exercise failure":
            raise
    else:
        raise AssertionError("Injected failure did not occur")


def run(store, repo, work):
    state = storage.load_state(store)
    if any(
        label not in {"fixtures", "exercise-A", "exercise-B"}
        for label in state["batches"]
    ):
        raise ValueError("Exercise requires a pre-release test environment")
    if state["published"] not in (None, "exercise-A", "exercise-B"):
        raise ValueError("Cannot exercise an initialized release")
    # Persist the explicit exercise marker so interrupted runs can be reset safely.
    marker = "_bump/exercise.json"
    if store.get("data", marker) is None:
        if state["batches"] or store.get("data", "metadata.json") is not None:
            raise ValueError("First exercise requires unused test buckets")
        storage.put_verified(
            store, "data", marker, b'{"exercise":true}', "application/json"
        )
    else:
        reset(store)
        storage.put_verified(
            store, "data", marker, b'{"exercise":true}', "application/json"
        )
    ordinary_manifest, ordinary = fixtures.generate(repo, work / "ordinary")
    storage.stage(
        store, "fixtures", ordinary_manifest, ordinary, work / "ordinary/files"
    )
    examples = {}
    for name in ("common", "removed", "new"):
        manifest, items = fixtures.generate(repo, work / name, name)
        examples[name] = (manifest, items)
    all_files = work / "exercise-files"
    all_files.mkdir(exist_ok=True)
    for name in ("ordinary", "common", "removed", "new"):
        for path in (work / name / "files").iterdir():
            shutil.copyfile(path, all_files / path.name)
    # A also includes a normal item: removing it from B must not remove its ordinary reference.
    a_items = sorted(
        examples["common"][1] + examples["removed"][1] + [ordinary[0]],
        key=lambda item: item["id"],
    )
    b_items = sorted(
        examples["common"][1] + examples["new"][1], key=lambda item: item["id"]
    )
    a_manifest = {"files": [{"md5": item["id"]} for item in a_items]}
    b_manifest = {"files": [{"md5": item["id"]} for item in b_items]}

    def publish_all(items):
        mixed = {item["id"]: item for item in ordinary + items}
        return publisher.run(
            store, repo, work, sorted(mixed.values(), key=lambda item: item["id"])
        )

    storage.stage(store, "exercise-A", a_manifest, a_items, all_files, "exercise-A")
    storage.publish(store, "exercise-A", publish_all)
    previous = store.get("data", "metadata.json")
    new_pdf = examples["new"][1][0]["id"] + ".pdf"
    failing_upload = FailOnce(store, "put", "backup", new_pdf)
    expect_failure(
        lambda: storage.stage(
            failing_upload, "exercise-B", b_manifest, b_items, all_files, "exercise-B"
        )
    )
    assert store.get("data", "metadata.json") == previous
    assert not storage.load_state(store)["batches"]["exercise-B"]["ready"]
    storage.stage(store, "exercise-B", b_manifest, b_items, all_files, "exercise-B")
    previous_sitemap = store.get("data", "sitemap.xml")

    def partial_publish(items):
        publish_all(items)
        # The unchanged binary uses its own S3 client. Restore the old sitemap to
        # reproduce the externally observable state of a second-object upload failure.
        store.put("data", "sitemap.xml", *previous_sitemap)
        raise OSError("Expected exercise failure")

    expect_failure(lambda: storage.publish(store, "exercise-B", partial_publish))
    try:
        storage.cleanup(store, set(), set(), {"exercise-A"})
    except ValueError as error:
        if "incomplete" not in str(error):
            raise
    else:
        raise AssertionError("Partial publication allowed deletion")
    storage.publish(store, "exercise-B", publish_all)
    removed = examples["removed"][1][0]["id"]
    failing_delete = FailOnce(store, "delete", "files", removed + ".jpg")
    expect_failure(
        lambda: storage.cleanup(failing_delete, set(), set(), {"exercise-A"})
    )
    assert storage.load_state(store)["pending_delete"]
    storage.cleanup(store, set(), set(), {"exercise-A"})
    for obj in storage.object_names([removed]):
        role, key, _ = storage.split_object(obj)
        assert store.get(role, key) is None
    for item in ordinary + b_items:
        for obj in storage.object_names([item["id"]]):
            role, key, _ = storage.split_object(obj)
            expected = (
                (all_files / key).read_bytes(),
                storage.TYPES[key.rsplit(".", 1)[1]],
            )
            assert store.get(role, key) == expected
    storage.publish(
        store,
        "exercise-B",
        lambda _: (_ for _ in ()).throw(AssertionError("Repeated publish")),
    )
    report = {
        "passed": [
            "upload retry",
            "partial publication blocks cleanup",
            "publication recovery",
            "partial delete retry",
            "ordinary and shared objects preserved",
            "idempotent publish",
        ],
        "publisher": publisher.CHECK_VERSION,
        "reset": True,
    }
    reset(store)
    sync.write_json(work / "exercise-report.json", report)
    print(json.dumps(report, ensure_ascii=False))


def reset(store):
    """Remove only this pre-release exercise's journaled objects and known roots."""
    state = storage.load_state(store)
    if any(
        label not in {"fixtures", "exercise-A", "exercise-B"}
        for label in state["batches"]
    ):
        raise ValueError("Release objects found; refusing exercise reset")
    if store.get("data", "_bump/exercise.json") is None:
        raise ValueError("Missing exercise ownership marker")
    # Remove public exercise roots first; the same resumable cleaner handles objects.
    for key in (
        "metadata.json",
        "sitemap.xml",
        "_bump/confirmed/metadata.json",
        "_bump/confirmed/sitemap.xml",
    ):
        store.delete("data", key)
    state["published"] = None
    state["publishing"] = None
    state["batches"] = {}
    storage.save_state(store, state)
    storage.cleanup(store, set(), set(), set(), datetime.now(UTC) + timedelta(days=2))
    store.delete("data", storage.STATE_KEY)
    store.delete("data", "_bump/exercise.json")
