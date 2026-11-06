# 大邮数学集 fork 同步测试

仅在 `Shxiao111/byrdocs-archive` 运行。读取正式版 RSS，按源 metadata 的物理页码重切北邮课程试卷，生成现有格式的 YAML、PDF 和封面，上传 Actions artifact 后在同一个 fork 创建 PR。脚本不包含生产存储上传、发布或删除操作。

## 运行

```sh
python -m pip install -r scripts/bump/requirements.txt
python -m unittest discover -s scripts/bump -p 'test_*.py' -v
python scripts/bump/sync.py build --version 1.5.0 --output output/bump
```

默认只写输出目录。`--apply` 在整批构建成功后替换本任务管理的 YAML 和 `sources/bump.json`；输出目录必须为空。可用 `--source-pdf` 和 `--source-metadata` 指定已下载的同版源文件。

在 Actions 中手动运行 **BUMP fork sync**；版本留空时从 RSS 选择最高正式版本。`retry` 允许重试已关闭或已合并版本，不覆盖待审 PR，也不允许降级。下载或构建失败可直接重跑。

启用 fork 的 Actions，并在 Actions / General / Workflow permissions 中开启允许 GitHub Actions 创建 PR 的选项。首次测试通过后将仓库变量 `BUMP_SCHEDULE_ENABLED` 设置为 `true`，每天 UTC 00:17（北京时间 08:17、日本时间 09:17）检查一次。无需生产 secrets。

## 处理规则

- 仅使用明确识别的课程、年份、考试阶段及标签。上/下册不推断学期，单年保留相同起止年份；日期按秋春学年转换，短学年按 2000 年代展开。明确的研究生标签和学院名写入对应选填字段。
- 缺少选填字段就省略。不能解析的必填字段、未知分类、作业和残卷拼合条目跳过；高考和附加材料排除。明细只放在 PR 正文，不提交问题清单或修正配置。
- 以答案标题或明确重启的答案编号识别答案；仅标记源文件中能识别出的内容。
- 保留原题页，附加原书完整许可页；PDF 属性和 `source.txt` 附件保存版本、来源、作者、原始页码和拆分说明。许可页无法定位时整批失败。
- 每版全部重切。最终字节 MD5 用于文件命名；不按标题去重。同一版本相同输入应生成相同文件。源页码越界、重叠、PDF 损坏及零有效条目均拒绝替换仓库内容。
- `sources/bump.json` 是内部所有权清单，不是公开元信息扩展。非本任务管理的同 MD5 元信息保持原样，在 PR 中报告。新版成功集合替换旧集合，跳过项不会回填旧版。

## 产物和边界

Artifact 包含单卷 PDF、JPG/WebP 封面、YAML、批次清单和仅预览的旧对象删除列表，保留 14 天。原书和解析临时文件不上传。原始标题在 PDF 和 PR 跳过明细中保留，不增加 YAML 字段，也不改主站的标题生成方式。

测试 YAML 继续使用现有 `https://byrdocs.org/files/<md5>.pdf` 格式。这些地址未上传生产存储，测试时应从 artifact 下载文件。

所有 PR API 明确指定 fork；推送前检查 origin。继承的生产发布、schema 上传、wiki 部署和线上 PR 检查仅允许上游仓库执行。生成任务自身执行离线检查，无需依赖机器人 PR 再触发工作流。

正式存储接入、合并后发布和实际清理是后续工作，本实现不会执行。不要将 artifact 中的 YAML 单独合入上游。
