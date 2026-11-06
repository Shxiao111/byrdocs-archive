"""Deterministic, self-authored PDFs for mixed-bucket regression exercises."""

import hashlib
import io

import jsonschema
import sync
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def make_pdf(label):
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        (
            "BT /F1 20 Tf 50 760 Td (BYR Docs storage test) Tj "
            "0 -40 Td /F1 14 Tf (" + label + ") Tj "
            "0 -35 Td (Self-authored fixture. Not real course material.) Tj ET"
        ).encode("ascii")
    )
    page.replace_contents(stream)
    writer.add_metadata({"/Title": label, "/Author": "BYR Docs fork storage tests"})
    output = io.BytesIO()
    writer.write(output)
    writer.close()
    return output.getvalue()


def generate(repo, out, variant="ordinary"):
    """Return a normal importer-shaped batch without adding public metadata files."""
    files = out / "files"
    metadata_dir = out / "metadata"
    files.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(exist_ok=True)
    if variant == "ordinary":
        examples = [
            (
                "Storage fixture textbook",
                "book",
                {
                    "title": "存储测试样例教材（自建）",
                    "authors": ["BYR Docs 测试"],
                    "isbn": ["9780306406157"],
                    "filetype": "pdf",
                },
            ),
            (
                "Storage fixture notes",
                "doc",
                {
                    "title": "存储测试样例资料（自建）",
                    "filetype": "pdf",
                    "course": [{"name": "存储测试"}],
                    "content": ["知识点"],
                },
            ),
            ("Storage fixture ordinary exam", "test", exam_data()),
        ]
    else:
        examples = [("Storage exercise " + variant, "test", exam_data())]
    items, entries = [], []
    for label, kind, data in examples:
        raw = make_pdf(label)
        digest = hashlib.md5(raw).hexdigest()
        pdf = files / f"{digest}.pdf"
        pdf.write_bytes(raw)
        sync.cover(pdf, files, digest)
        item = {
            "id": digest,
            "url": f"https://byrdocs.org/files/{digest}.pdf",
            "type": kind,
            "data": data,
        }
        schema = sync.strict_schema(
            sync.YAML_CODEC.load(
                (repo / f"schema/{kind}.yaml").read_text(encoding="utf-8")
            )
        )
        jsonschema.Draft7Validator(schema).validate(item)
        (metadata_dir / f"{digest}.yml").write_text(
            sync.yaml_text(item), encoding="utf-8"
        )
        items.append(item)
        entries.append({"md5": digest})
    manifest = {
        "version": "fixture-" + variant,
        "files": entries,
        "managed_metadata": sorted(item["id"] for item in items),
    }
    sync.write_json(out / "manifest.json", manifest)
    return manifest, sorted(items, key=lambda item: item["id"])


def exam_data():
    return {
        "course": {"name": "存储测试"},
        "time": {"start": "2025", "end": "2026", "stage": "期末"},
        "filetype": "pdf",
        "content": ["原题"],
    }
