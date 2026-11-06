"""Run the pinned, unmodified BYR Docs publisher against test resources."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pypdfium2_raw
import storage
import sync

CHECK_VERSION = "v6.5.1"
TEST_SITE = "http://127.0.0.1:18765"


class EmptyUploadAPI(BaseHTTPRequestHandler):
    """Only accepts the two empty-upload requests the pinned publisher requires."""

    def respond(self, allowed):
        if not allowed:
            self.server.unexpected.append((self.command, self.path))
        body = json.dumps({"success": allowed, "files": []}).encode()
        self.send_response(200 if allowed else 400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.respond(self.path == "/api/file/notPublished")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.respond(self.path == "/api/file/publish" and body == b'{"ids":[]}')

    def log_message(self, *_):
        pass


@contextmanager
def local_api(port=18765):
    server = ThreadingHTTPServer(("127.0.0.1", port), EmptyUploadAPI)
    server.unexpected = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
        if server.unexpected:
            raise ValueError(f"Unexpected publisher requests: {server.unexpected}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def install(out):
    """Download a fixed upstream executable; use the pinned Python PDFium library."""
    binary = out / "tools/upload-metadata"
    binary.parent.mkdir(parents=True, exist_ok=True)
    if not binary.exists():
        sync.download(
            f"https://github.com/byrdocs/byrdocs-check/releases/download/{CHECK_VERSION}/upload-metadata",
            binary,
        )
        binary.chmod(0o755)
    return binary.resolve()


def run(store, repo, work, items):
    """Publish existing-schema YAML via the real binary, then validate its output."""
    if os.name != "posix":
        raise ValueError("The official publisher runs on Linux; use GitHub Actions")
    binary = install(work)
    # Each execution gets a fresh git root: sitemap requires tracked YAML timestamps.
    with tempfile.TemporaryDirectory(prefix="publisher-", dir=work) as temp:
        root = Path(temp)
        metadata = root / "metadata"
        metadata.mkdir()
        for item in items:
            (metadata / f"{item['id']}.yml").write_text(
                sync.yaml_text(item), encoding="utf-8"
            )
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "metadata"], check=True)
        commit_date = subprocess.check_output(
            ["git", "-C", str(repo), "show", "-s", "--format=%cI", "HEAD"], text=True
        ).strip()
        git_env = {
            **os.environ,
            "GIT_AUTHOR_DATE": commit_date,
            "GIT_COMMITTER_DATE": commit_date,
        }
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=BUMP storage test",
                "-c",
                "user.email=storage-test@invalid.example",
                "commit",
                "--quiet",
                "-m",
                "Test publication input",
            ],
            check=True,
            env=git_env,
        )
        (root / "lib").mkdir()
        shutil.copyfile(
            Path(pypdfium2_raw.__file__).parent / "libpdfium.so",
            root / "lib/libpdfium.so",
        )
        config = store.config
        env = {
            **os.environ,
            "METADATA_DIR": str(metadata),
            "R2_ACCOUNT_ID": config["account"],
            "R2_ACCESS_KEY_ID": config["access_key"],
            "R2_SECRET_ACCESS_KEY": config["secret_key"],
            "R2_FILE_BUCKET": storage.BUCKETS["files"],
            "R2_DATA_BUCKET": storage.BUCKETS["data"],
            "BACKUP_ENDPOINT": config["backup_endpoint"],
            "BACKUP_ENDPOINT_REGION": config["backup_region"],
            "BACKUP_ACCESS_KEY_ID": config["access_key"],
            "BACKUP_SECRET_ACCESS_KEY": config["secret_key"],
            "BACKUP_FILE_BUCKET": storage.BUCKETS["backup"],
            "BYRDOCS_SITE_URL": TEST_SITE,
            "BYRDOCS_SITE_TOKEN": "local-test-only",
            "FILELIST_SITE_URL": TEST_SITE,
            "BYRDOCS_SITE_REPO_OWNER": "Shxiao111",
            "BYRDOCS_SITE_REPO_NAME": "byrdocs",
        }
        with local_api():
            subprocess.run([str(binary)], cwd=root, env=env, check=True, timeout=900)
        raw = (metadata / "metadata.json").read_bytes()
        actual = json.loads(raw)
        if {item["id"] for item in actual} != {item["id"] for item in items} or len(
            actual
        ) != len(items):
            raise ValueError("Publisher changed the set of entries")
        for item in actual:
            pdf = store.get("files", f"{item['id']}.{item['data']['filetype']}")
            if pdf is None or item["data"]["filesize"] != len(pdf[0]):
                raise ValueError("Published file size differs from stored object")
        sitemap = (metadata / "sitemap.xml").read_bytes()
        locations = [
            node.text
            for node in ET.fromstring(sitemap).iter(
                "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
            )
        ]
        if set(locations) != {
            TEST_SITE,
            TEST_SITE + "/about",
            *[f"{TEST_SITE}/?q={item['id']}" for item in items],
        }:
            raise ValueError("Unexpected sitemap locations")
        return {
            "metadata.json": (raw, "application/json"),
            "sitemap.xml": (sitemap, "application/xml"),
        }
