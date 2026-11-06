"""Isolated R2 object ownership, publication confirmation and resumable cleanup."""

import json
import os
import re
from datetime import UTC, datetime

import boto3
import sync
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKETS = {
    "files": "byrdocs-bump-test-files",
    "data": "byrdocs-bump-test-data",
    "backup": "byrdocs-bump-test-backup",
}
STATE_KEY = "_bump/state.json"
TYPES = {"pdf": "application/pdf", "jpg": "image/jpeg", "webp": "image/webp"}


def now():
    return datetime.now(UTC).isoformat()


def settings(env):
    """Validate the fork and all destinations before constructing cloud clients."""
    sync.assert_fork(env["GITHUB_REPOSITORY"])
    if env["BUMP_TEST_STORAGE_ENABLED"] != "true":
        raise ValueError("BUMP_TEST_STORAGE_ENABLED must be true")
    account = env["BUMP_TEST_ACCOUNT_ID"]
    if not re.fullmatch(r"[0-9a-f]{32}", account):
        raise ValueError("Invalid test account ID")
    for role, bucket in BUCKETS.items():
        if env[f"BUMP_TEST_{role.upper()}_BUCKET"] != bucket:
            raise ValueError(f"Only the dedicated {role} test bucket is allowed")
    endpoint = f"https://{account}.r2.cloudflarestorage.com"
    if env["BUMP_TEST_BACKUP_ENDPOINT"] != endpoint:
        raise ValueError("The test backup must use this test R2 account")
    if env["BUMP_TEST_BACKUP_REGION"] != "auto":
        raise ValueError("The test R2 backup region must be auto")
    return {
        "account": account,
        "endpoint": endpoint,
        "access_key": env["BUMP_TEST_ACCESS_KEY_ID"],
        "secret_key": env["BUMP_TEST_SECRET_ACCESS_KEY"],
        "backup_endpoint": env["BUMP_TEST_BACKUP_ENDPOINT"],
        "backup_region": env["BUMP_TEST_BACKUP_REGION"],
    }


class R2:
    """Only accesses the three explicitly configured private test buckets."""

    def __init__(self, config):
        self.config = config
        options = {
            "aws_access_key_id": config["access_key"],
            "aws_secret_access_key": config["secret_key"],
            "config": Config(
                retries={"mode": "standard", "max_attempts": 3},
                connect_timeout=15,
                read_timeout=120,
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        }
        self.clients = {
            "main": boto3.client(
                "s3", endpoint_url=config["endpoint"], region_name="auto", **options
            ),
            "backup": boto3.client(
                "s3",
                endpoint_url=config["backup_endpoint"],
                region_name=config["backup_region"],
                **options,
            ),
        }

    def client(self, role):
        return self.clients["backup" if role == "backup" else "main"]

    def get(self, role, key):
        try:
            response = self.client(role).get_object(Bucket=BUCKETS[role], Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        with response["Body"] as body:
            return body.read(), response["ContentType"]

    def put(self, role, key, body, content_type):
        self.client(role).put_object(
            Bucket=BUCKETS[role], Key=key, Body=body, ContentType=content_type
        )

    def delete(self, role, key):
        self.client(role).delete_object(Bucket=BUCKETS[role], Key=key)


def load_state(store):
    value = store.get("data", STATE_KEY)
    if value is None:
        return {
            "batches": {},
            "owned": [],
            "published": None,
            "publishing": None,
            "pending_delete": [],
        }
    state = json.loads(value[0])
    # Cloud state is the ownership boundary for deletions. Reject arbitrary keys.
    for field in ("owned", "pending_delete"):
        for obj in state[field]:
            split_object(obj)
    for batch in state["batches"].values():
        if not batch["ids"] or any(
            not re.fullmatch(sync.MD5, digest) for digest in batch["ids"]
        ):
            raise ValueError("Invalid batch IDs in cloud state")
    for field in ("published", "publishing"):
        if state[field] is not None and state[field] not in state["batches"]:
            raise ValueError("Publication refers to an absent batch")
    return state


def save_state(store, state):
    body = json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
    put_verified(store, "data", STATE_KEY, body, "application/json")


def put_verified(store, role, key, body, content_type):
    store.put(role, key, body, content_type)
    if store.get(role, key) != (body, content_type):
        raise ValueError(f"Read-back mismatch: {role}/{key}")


def split_object(obj):
    match = re.fullmatch(rf"(files|backup)/({sync.MD5})\.(pdf|jpg|webp)", obj)
    if not match or (match[1] == "backup" and match[3] != "pdf"):
        raise ValueError(f"Invalid managed object: {obj}")
    return match[1], obj.split("/", 1)[1], match[2]


def object_names(ids):
    return [
        f"{role}/{digest}.{ext}"
        for digest in sorted(ids)
        for role, ext in (
            ("files", "pdf"),
            ("files", "jpg"),
            ("files", "webp"),
            ("backup", "pdf"),
        )
    ]


def stage(store, label, manifest, metadata, files, branch=None):
    """Journal ownership before writes; retries verify or fill the same batch."""
    ids = sorted({entry["md5"] for entry in manifest["files"]})
    if not ids or any(not re.fullmatch(sync.MD5, digest) for digest in ids):
        raise ValueError("Empty or invalid batch")
    if sorted(item["id"] for item in metadata) != ids:
        raise ValueError("Metadata and batch IDs differ")
    state = load_state(store)
    if state["publishing"] is not None and state["publishing"] != label:
        raise ValueError(
            "Finish the interrupted publication before uploading another batch"
        )
    if label in state["batches"]:
        batch = state["batches"][label]
        if batch["manifest"] != manifest or batch["metadata"] != metadata:
            raise ValueError("The same batch changed; refusing an implicit replacement")
    else:
        state["batches"][label] = {
            "manifest": manifest,
            "metadata": metadata,
            "ids": ids,
            "created_at": now(),
            "branch": branch,
            "ready": False,
        }
        save_state(store, state)
    for obj in object_names(ids):
        role, key, _ = split_object(obj)
        body = (files / key).read_bytes()
        content_type = TYPES[key.rsplit(".", 1)[1]]
        existing = store.get(role, key)
        if existing is not None:
            if existing != (body, content_type):
                raise ValueError(f"Existing object differs: {obj}")
            continue
        if obj not in state["owned"]:
            state["owned"].append(obj)
            save_state(store, state)
        put_verified(store, role, key, body, content_type)
    state["batches"][label]["ready"] = True
    save_state(store, state)
    return label


def publish(store, label, publisher):
    """Confirm both conventional root objects before marking a batch published."""
    state = load_state(store)
    batch = state["batches"][label]
    if state["published"] == label and state["publishing"] is None:
        verify_publication(store)
        return
    if not batch["ready"] or (
        state["publishing"] is not None and state["publishing"] != label
    ):
        raise ValueError("Batch is incomplete or another publication needs recovery")
    state["publishing"] = label
    save_state(store, state)
    # Publisher is the unchanged upstream executable, not a JSON reimplementation.
    expected = publisher(batch["metadata"])
    for key, (body, content_type) in expected.items():
        if store.get("data", key) != (body, content_type):
            raise ValueError(f"Publication read-back mismatch: {key}")
    if set(expected) != {"metadata.json", "sitemap.xml"}:
        raise ValueError("Publisher must confirm metadata.json and sitemap.xml")
    for key, (body, content_type) in expected.items():
        put_verified(store, "data", "_bump/confirmed/" + key, body, content_type)
    state["published"] = label
    state["publishing"] = None
    save_state(store, state)


def verify_publication(store):
    for key in ("metadata.json", "sitemap.xml"):
        confirmed = store.get("data", "_bump/confirmed/" + key)
        if confirmed is None or store.get("data", key) != confirmed:
            raise ValueError(f"Published object changed or is missing: {key}")


def metadata_refs(items):
    refs = set()
    for item in items:
        if not re.fullmatch(sync.MD5, item["id"]):
            raise ValueError("Invalid metadata ID")
        refs.add(item["id"])
        match = re.fullmatch(
            rf"https://byrdocs\.org/files/({sync.MD5})\.(pdf|zip)", item["url"]
        )
        if not match:
            raise ValueError("Invalid metadata URL")
        refs.add(match[1])
    return refs


def cleanup(store, references, open_branches, closed_branches, timestamp=None):
    """Delete only journaled, unreferenced objects; keep interrupted work durable."""
    state = load_state(store)
    if state["publishing"] is not None:
        raise ValueError("Publication incomplete; cleanup prohibited")
    if state["published"] is not None:
        verify_publication(store)
    timestamp = datetime.now(UTC) if timestamp is None else timestamp
    protected = set(references)
    published = store.get("data", "metadata.json")
    if published is not None:
        protected |= metadata_refs(json.loads(published[0]))
    keep = set()
    for label, batch in state["batches"].items():
        age = (timestamp - datetime.fromisoformat(batch["created_at"])).total_seconds()
        if (
            label == "fixtures"
            or label == state["published"]
            or batch["branch"] in open_branches
            or (batch["branch"] not in closed_branches and age < 86400)
        ):
            protected.update(batch["ids"])
            keep.add(label)
    candidates = {
        obj for obj in state["owned"] if split_object(obj)[2] not in protected
    }
    # Recompute after every restart, so new references cancel prior delete candidates.
    state["pending_delete"] = sorted(candidates)
    save_state(store, state)
    for obj in list(state["pending_delete"]):
        role, key, _ = split_object(obj)
        store.delete(role, key)
        if store.get(role, key) is not None:
            raise ValueError(f"Deletion not confirmed: {obj}")
        state["owned"].remove(obj)
        state["pending_delete"].remove(obj)
        save_state(store, state)
    state["batches"] = {
        label: batch for label, batch in state["batches"].items() if label in keep
    }
    save_state(store, state)
    return len(candidates)


def connect():
    return R2(settings(os.environ))
