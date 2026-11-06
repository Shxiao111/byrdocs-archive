"""Fork-only upload, real-publisher integration and daily recovery orchestration."""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import exercise
import fixtures
import publisher
import storage
import sync


def read_metadata(directory):
    return sorted(
        [
            sync.YAML_CODEC.load(path.read_text(encoding="utf-8"))
            for path in directory.glob("*.y*ml")
        ],
        key=lambda item: item["id"],
    )


def repository_refs(repo):
    return storage.metadata_refs(read_metadata(repo / "metadata"))


def require_current_default(repo):
    """Do not publish or clean from a checkout superseded during a long upload."""
    checkout = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "refs/remotes/origin/master"], text=True
    ).strip()
    current = json.loads(sync.gh("api", f"repos/{sync.FORK}/git/ref/heads/master"))[
        "object"
    ]["sha"]
    if checkout != current:
        raise ValueError(
            "Default branch changed during this run; rerun on the current merged data"
        )


def pull_requests():
    pages = json.loads(
        sync.gh(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{sync.FORK}/pulls?state=all&per_page=100",
        )
    )
    return [
        pr
        for page in pages
        for pr in page
        if pr["head"]["ref"].startswith(sync.BRANCH_PREFIX)
    ]


def pr_references(prs):
    refs, opened, closed = set(), set(), set()
    for pr in prs:
        branch = pr["head"]["ref"]
        if pr["state"] != "open":
            closed.add(branch)
            continue
        opened.add(branch)
        payload = json.loads(
            sync.gh(
                "api",
                f"repos/{sync.FORK}/contents/sources/bump.json?ref={pr['head']['sha']}",
            )
        )
        manifest = json.loads(base64.b64decode(payload["content"]))
        ids = {entry["md5"] for entry in manifest["files"]}
        if not ids or any(not re.fullmatch(sync.MD5, digest) for digest in ids):
            raise ValueError("Invalid pending PR manifest")
        refs.update(ids)
    return refs, opened, closed


def maintenance(store, repo, prs):
    refs, opened, closed = pr_references(prs)
    require_current_default(repo)
    count = storage.cleanup(store, repository_refs(repo) | refs, opened, closed)
    print(f"清理完成：{count} 个无引用的受管理对象")


def validate_build(repo, out, committed=False):
    manifest = sync.read_json(out / "manifest.json")
    ids = {entry["md5"] for entry in manifest["files"]}
    items = read_metadata(out / "metadata")
    if not ids or ids != {item["id"] for item in items}:
        raise ValueError("Build metadata does not match manifest")
    for item in items:
        path = out / "files" / f"{item['id']}.pdf"
        if hashlib.md5(path.read_bytes()).hexdigest() != item["id"]:
            raise ValueError("PDF MD5 differs from the manifest")
    if committed:
        previous = sync.read_json(repo / sync.MANIFEST)
        if previous != manifest:
            raise ValueError("Rebuilt manifest differs from the merged manifest")
        for item in items:
            existing = sync.YAML_CODEC.load(
                (repo / "metadata" / f"{item['id']}.yml").read_text(encoding="utf-8")
            )
            if item["id"] in manifest["managed_metadata"] and existing != item:
                raise ValueError(
                    "Rebuilt managed metadata differs from the merged YAML"
                )
        # Preserve existing metadata for source files already owned by other entries.
        items = [
            sync.YAML_CODEC.load(
                (repo / "metadata" / f"{digest}.yml").read_text(encoding="utf-8")
            )
            for digest in sorted(ids)
        ]
    return manifest, items


def build(repo, out, version, apply=False):
    sync.build(
        SimpleNamespace(
            repo=repo,
            output=out,
            version=version,
            source_pdf=None,
            source_metadata=None,
            apply=apply,
        )
    )
    return validate_build(repo, out, committed=not apply)


def ensure_fixtures(store, repo, work):
    out = work / "ordinary"
    manifest, items = fixtures.generate(repo, out)
    storage.stage(store, "fixtures", manifest, items, out / "files")
    return items


def stage_release(store, manifest, items, files):
    """Keep same-version rebuilds separate until their replacement PR is published."""
    batches = storage.load_state(store)["batches"]
    base = "release-" + manifest["version"]
    for label, batch in batches.items():
        if batch["manifest"] == manifest and batch["metadata"] == items:
            break
    else:
        label = base
        revision = 1
        while label in batches:
            revision += 1
            label = f"{base}-{revision}"
    return storage.stage(
        store, label, manifest, items, files, sync.BRANCH_PREFIX + manifest["version"]
    )


def publish_batch(store, repo, work, label):
    state = storage.load_state(store)
    ordinary = state["batches"]["fixtures"]["metadata"]

    def publish_all(items):
        combined = {item["id"]: item for item in ordinary}
        combined.update({item["id"]: item for item in items})
        require_current_default(repo)
        return publisher.run(
            store, repo, work, sorted(combined.values(), key=lambda item: item["id"])
        )

    storage.publish(store, label, publish_all)


def recover(store, repo, work):
    state = storage.load_state(store)
    if state["publishing"] is not None:
        publish_batch(store, repo, work, state["publishing"])


def bootstrap(store, repo, work):
    if store.get("data", "_bump/exercise.json") is not None:
        raise ValueError(
            "Finish the interrupted exercise before initializing a release"
        )
    recover(store, repo, work)
    ensure_fixtures(store, repo, work)
    committed = sync.read_json(repo / sync.MANIFEST)
    out = work / "release"
    manifest, items = build(repo, out, committed["version"])
    label = stage_release(store, manifest, items, out / "files")
    for path in sorted((repo / "schema").glob("*.y*ml")):
        storage.put_verified(
            store, "data", path.name, path.read_bytes(), "application/yaml"
        )
    publish_batch(store, repo, work, label)
    maintenance(store, repo, pull_requests())
    print(f"已确认发布 {manifest['version']}：{len(items)} 份大邮试卷，加 3 份普通样例")


def sync_release(store, repo, work, requested, retry):
    recover(store, repo, work)
    committed = sync.read_json(repo / sync.MANIFEST)
    state = storage.load_state(store)
    if state["published"] is None:
        raise ValueError("先手动运行 publish，完成已合并版本的首次存储验收")
    if state["batches"][state["published"]]["manifest"] != committed:
        bootstrap(store, repo, work)
    prs = pull_requests()
    maintenance(store, repo, prs)
    if requested:
        version = requested
    else:
        request = urllib.request.Request(
            sync.PUB + "/rss", headers={"User-Agent": "Mozilla/5.0 BYRDocs-Bump-Sync"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            version = sync.release_from_feed(response.read())
    if not re.fullmatch(sync.VERSION, version):
        raise ValueError("Invalid formal version")
    should_build, reason = sync.should_build(version, committed, prs, retry)
    print(reason)
    if not should_build:
        return
    out = work / "candidate"
    manifest, items = build(repo, out, version, apply=True)
    # Use retained repository metadata where a same-MD5 non-BUMP entry exists.
    items = [
        sync.YAML_CODEC.load(
            (repo / "metadata" / f"{item['id']}.yml").read_text(encoding="utf-8")
        )
        for item in items
    ]
    stage_release(store, manifest, items, out / "files")
    body = out / "pr-body.md"
    with body.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n## 私有测试存储\n\nPDF、JPG/WebP 封面及 PDF 备份均已上传测试桶并读回校验。"
            "合并后使用原版发布程序更新混合元信息，再清理无引用旧对象；主站下载地址未启用。\n"
        )
    # Artifact upload precedes PR creation in the workflow.
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("create_pr=true\n")


def probe(store):
    key = "_bump-connection-probe.txt"
    expected = (b"BYR Docs dedicated test bucket probe\n", "text/plain")
    for role in storage.BUCKETS:
        existing = store.get(role, key)
        if existing is not None and existing != expected:
            raise ValueError("Unexpected connection probe contents")
        storage.put_verified(store, role, key, *expected)
        store.delete(role, key)
        if store.get(role, key) is not None:
            raise ValueError("Probe deletion failed")
    print("三个测试桶的上传、读回和删除均成功")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("probe", "exercise", "publish", "sync"))
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    remote = subprocess.check_output(
        ["git", "-C", str(args.repo), "remote", "get-url", "origin"], text=True
    ).strip()
    if remote not in {
        f"https://github.com/{sync.FORK}",
        f"https://github.com/{sync.FORK}.git",
        f"git@github.com:{sync.FORK}.git",
    }:
        raise ValueError("Origin must be the dedicated fork")
    store = storage.connect()
    if args.action == "probe":
        probe(store)
    elif args.action == "exercise":
        if os.environ["BUMP_SCHEDULE_ENABLED"] != "false":
            raise ValueError("Pause BUMP_SCHEDULE_ENABLED before exercising")
        exercise.run(store, args.repo, args.output)
    elif args.action == "publish":
        bootstrap(store, args.repo, args.output)
    else:
        sync_release(store, args.repo, args.output, args.version, args.retry)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
