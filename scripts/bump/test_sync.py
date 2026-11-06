"""Persistent contracts for the fork importer: admission, format and ownership."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema
import sync
from pypdf.errors import PdfReadError

ROOT = Path(__file__).resolve().parents[2]


def entry(
    title="2024–2025 学年第一学期《高等数学 A（上）》期末考试试题（A 卷）",
    tags=None,
    page=2,
    length=2,
):
    return {
        "title": title,
        "tags": ["分析上期末"] if tags is None else tags,
        "page-physical": page,
        "length-in-page": length,
    }


class MetadataTests(unittest.TestCase):
    def test_titles_produce_only_existing_schema_fields(self):
        cases = [
            (entry(), "高等数学A（上）", "First"),
            (
                entry("2024–2025《工科数学分析（上）》期中考试试题", []),
                "工科数学分析（上）",
                None,
            ),
            (
                entry("2017–2018 数分（下）期中试题", ["分析下期中", "无准确标题"]),
                "数学分析（下）",
                None,
            ),
            (
                entry("2020–2021 学年第一学期《高等数学 A》（上）期末考试试题（A1）"),
                "高等数学A（上）",
                "First",
            ),
            (
                entry(
                    "《矩阵分析与应用》期末考试试题 2011/2012 学年",
                    ["研究生课", "矩阵论期末", "无准确标题"],
                ),
                "矩阵分析与应用",
                None,
            ),
        ]
        schema = sync.strict_schema(
            sync.YAML_CODEC.load(
                (ROOT / "schema/test.yaml").read_text(encoding="utf-8")
            )
        )
        validator = jsonschema.Draft7Validator(schema)
        for source, course, semester in cases:
            with self.subTest(title=source["title"]):
                data = sync.parse_entry(source)
                self.assertEqual(data["course"]["name"], course)
                self.assertEqual(data["time"].get("semester"), semester)
                value = {
                    "id": "a" * 32,
                    "url": "https://byrdocs.org/files/" + "a" * 32 + ".pdf",
                    "type": "test",
                    "data": data,
                }
                validator.validate(value)
                data["title"] = "must not extend metadata"
                self.assertFalse(validator.is_valid(value))

    def test_dates_and_missing_semester(self):
        for title, expected in [
            ("高等数学期末试题 (A) 2004.1", ("2003", "2004", "First")),
            ("高级数理逻辑 2008 年 12 月", ("2008", "2009", "First")),
            ("高数 B（下）2026 春期中", ("2025", "2026", "Second")),
            ("24–25 学年秋季学期高等代数（上）期中考试", ("2024", "2025", "First")),
            ("2017 高等数学（上）期中试卷", ("2017", "2017", None)),
            ("2018–2019 学年线性代数期末试题（A）", ("2018", "2019", None)),
        ]:
            with self.subTest(title=title):
                time = sync.parse_time(title)
                self.assertEqual(
                    (time["start"], time["end"], time.get("semester")), expected
                )

    def test_unparseable_and_out_of_scope_entries_are_skippable(self):
        for source in [
            entry("高等数学期末考试"),
            entry("2024–2025《未知课程》期末"),
            entry("2024–2025 高等数学学习笔记", []),
            entry(tags=["新分类"]),
            entry(tags=["高考"]),
            entry(tags=["附加"]),
            entry("2022–2023 学年第 1 学期《运筹学》期末限时作业", ["运筹学期末"]),
            entry("2024–2025 高等数学期中", ["分析上期末"]),
        ]:
            with self.subTest(source=source), self.assertRaises(sync.SkipPaper):
                sync.parse_entry(source)

    def test_research_and_explicit_college(self):
        data = sync.parse_entry(
            entry(
                "2024–2025 第一学期《概率论与随机过程》期末", ["研究生课", "研概随期末"]
            )
        )
        self.assertEqual(data["course"]["type"], "研究生")
        data = sync.parse_entry(
            entry(
                "2019–2020 学年第 2 学期《概率论与数理统计》试题（B 卷，自动化学院用）",
                ["概统期末"],
            )
        )
        self.assertEqual(data["college"], ["自动化学院"])
        self.assertEqual(data["time"]["stage"], "期末")


class ReleaseTests(unittest.TestCase):
    def test_feed_ignores_preview_and_unrelated_updates(self):
        xml = "<rss><channel><item><title>大邮数学集快照版 261105</title><guid>https://pub.fa-555.com/rss#bump-preview-261105</guid></item><item><title>大邮数学集正式版 1.5.0 更新</title><guid>https://pub.fa-555.com/rss#bump-1.5.0</guid></item><item><title>其他项目正式版 9.0.0</title></item><item><title>大邮数学集正式版 1.4.3 更新</title><guid>https://pub.fa-555.com/rss#bump-1.4.3</guid></item></channel></rss>"
        self.assertEqual(sync.release_from_feed(xml), "1.5.0")
        with self.assertRaises(ValueError):
            sync.release_from_feed("<rss><channel /></rss>")

    def test_repeated_closed_open_and_downgrade_runs(self):
        closed = [{"state": "closed", "head": {"ref": "automation/bump-1.5.0"}}]
        opened = [{"state": "open", "head": {"ref": "automation/bump-1.4.3"}}]
        cases = [
            ("1.5.0", None, [], False, True),
            ("1.5.0", {"version": "1.5.0"}, [], False, False),
            ("1.5.0", None, closed, False, False),
            ("1.5.0", None, closed, True, True),
            ("1.5.0", None, opened, True, False),
            ("1.4.3", {"version": "1.5.0"}, [], True, False),
        ]
        for version, manifest, prs, retry, expected in cases:
            with self.subTest(version=version, manifest=manifest, prs=prs):
                self.assertEqual(
                    sync.should_build(version, manifest, prs, retry)[0], expected
                )

    def test_source_bounds_and_overlap_are_fatal(self):
        source = {
            "config": {"release-type": "release", "version-number": "1.5.0"},
            "problemsets": [entry()],
        }
        reader = SimpleNamespace(pages=[None] * 4)
        self.assertEqual(len(sync.validate_source(source, reader, "1.5.0")), 1)
        for changes in [
            {"problemsets": [entry(page=0)]},
            {"problemsets": [entry(page=4, length=2)]},
            {"problemsets": [entry(), entry(page=3)]},
            {"config": {"release-type": "preview", "version-number": "1.5.0"}},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                sync.validate_source({**source, **changes}, reader, "1.5.0")


class OwnershipTests(unittest.TestCase):
    def test_zero_valid_entries_and_corrupt_pdf_leave_repository_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            (repo / "metadata").mkdir(parents=True)
            (repo / "schema").mkdir()
            (repo / "schema/test.yaml").write_bytes(
                (ROOT / "schema/test.yaml").read_bytes()
            )
            original = repo / "metadata" / ("a" * 32 + ".yml")
            original.write_text("unchanged", encoding="utf-8")
            source_path = root / "source.json"
            source_path.write_text(
                json.dumps(
                    {
                        "config": {
                            "release-type": "release",
                            "version-number": "1.5.0",
                        },
                        "problemsets": [entry("未知课程", [], length=1)],
                    }
                ),
                encoding="utf-8",
            )
            pdf = root / "bad.pdf"
            pdf.write_bytes(b"not a PDF")
            args = SimpleNamespace(
                repo=repo,
                output=root / "zero",
                source_pdf=pdf,
                source_metadata=source_path,
                version="1.5.0",
                apply=True,
            )
            reader = SimpleNamespace(
                pages=[
                    SimpleNamespace(
                        extract_text=lambda: "版权声明与授权许可 CC BY-NC-SA 4.0"
                    ),
                    None,
                ]
            )
            with (
                patch.object(sync, "PdfReader", return_value=reader),
                self.assertRaisesRegex(ValueError, "没有有效条目"),
            ):
                sync.build(args)
            args.output = root / "corrupt"
            with self.assertRaises(PdfReadError):
                sync.build(args)
            self.assertEqual(original.read_text(encoding="utf-8"), "unchanged")
            self.assertFalse((repo / sync.MANIFEST).exists())

    def test_replace_only_owned_metadata_and_preview_unreferenced_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "metadata").mkdir()
            old, shared, new = "a" * 32, "b" * 32, "c" * 32

            def item(digest):
                return {
                    "id": digest,
                    "url": f"https://byrdocs.org/files/{digest}.pdf",
                    "type": "test",
                    "data": sync.parse_entry(entry()),
                }

            for digest in (old, shared):
                (root / "metadata" / f"{digest}.yml").write_text(
                    sync.yaml_text(item(digest)), encoding="utf-8"
                )
            previous = {
                "managed_metadata": [old],
                "files": [{"md5": old}, {"md5": shared}],
            }
            managed, reused, changes, removed, cleanup = sync.replacement(
                root, previous, {new: item(new), shared: item(shared)}
            )
            self.assertEqual(managed, [new])
            self.assertEqual(reused, [shared])
            self.assertEqual(set(changes), {new})
            self.assertEqual(removed, [old])
            self.assertEqual(cleanup, [old])
            self.assertTrue((root / "metadata" / f"{old}.yml").exists())
            self.assertTrue((root / "metadata" / f"{shared}.yml").exists())

    def test_public_repo_is_rejected_by_fork_boundary(self):
        sync.assert_fork("Shxiao111/byrdocs-archive")
        with self.assertRaises(ValueError):
            sync.assert_fork("byrdocs/byrdocs-archive")

    def test_workflows_cannot_publish_from_fork(self):
        for name, job in [
            ("upload-metadata.yml", "update"),
            ("upload-schema.yml", "update"),
            ("deploy-wiki-site.yml", "deploy"),
            ("check-pr-format.yml", "check-format"),
        ]:
            workflow = sync.YAML_CODEC.load(
                (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            )
            self.assertIn(
                "github.repository == 'byrdocs/byrdocs-archive'",
                workflow["jobs"][job]["if"],
            )
        workflow = (ROOT / ".github/workflows/bump-sync.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("secrets.", workflow)
        self.assertIn("BUMP_SCHEDULE_ENABLED", workflow)


if __name__ == "__main__":
    unittest.main()
