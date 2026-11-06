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

启用 fork 的 Actions，并在 Actions / General / Workflow permissions 中开启允许 GitHub Actions 创建 PR 的选项。首次测试通过后将仓库变量 `BUMP_SCHEDULE_ENABLED` 设置为 `true`，每天 UTC 21:17（北京时间次日 05:17、日本时间次日 06:17，与生存指南 freshatlas 同步时间一致）检查一次。无需生产 secrets。

## 处理规则

- 仅使用明确识别的课程、年份、考试阶段及标签。上/下册不推断学期，单年保留相同起止年份；日期按秋春学年转换，短学年按 2000 年代展开。明确的研究生标签和学院名写入对应选填字段。
- 缺少选填字段就省略。不能解析的必填字段、未知分类、作业和残卷拼合条目跳过；高考和附加材料排除。明细只放在 PR 正文，不提交问题清单或修正配置。
- 以答案标题或明确重启的答案编号识别答案；仅标记源文件中能识别出的内容。
- 按物理页码保留原题页，输出页数等于源 metadata 中的试卷页数。PDF 属性和 `source.txt` 附件继续保存版本、来源、作者、原始页码和拆分说明。
- 每版全部重切。最终字节 MD5 用于文件命名；不按标题去重。同一版本相同输入应生成相同文件。源页码越界、重叠、PDF 损坏及零有效条目均拒绝替换仓库内容。
- `sources/bump.json` 是内部所有权清单，不是公开元信息扩展。非本任务管理的同 MD5 元信息保持原样，在 PR 中报告。新版成功集合替换旧集合，跳过项不会回填旧版。

## 产物和边界

Artifact 包含单卷 PDF、JPG/WebP 封面、YAML、批次清单和仅预览的旧对象删除列表，保留 14 天。原书和解析临时文件不上传。原始标题在 PDF 和 PR 跳过明细中保留，不增加 YAML 字段，也不改主站的标题生成方式。

测试 YAML 继续使用现有 `https://byrdocs.org/files/<md5>.pdf` 格式。这些地址未上传生产存储，测试时应从 artifact 下载文件。

所有 PR API 明确指定 fork；推送前检查 origin。继承的生产发布、schema 上传、wiki 部署和线上 PR 检查仅允许上游仓库执行。生成任务自身执行离线检查，无需依赖机器人 PR 再触发工作流。

生产存储接入仍未启用。不要将 artifact 中的 YAML 单独合入上游。

## 私有 R2 测试环境

`BUMP_TEST_STORAGE_ENABLED=true` 后使用 `bump-storage.yml`，替代 artifact-only 同步任务。
三个桶只模拟真实存储布局，不部署主站。正式文件位于桶根目录，不带版本前缀：

| 桶 | 内容 |
|---|---|
| `byrdocs-bump-test-files` | `<md5>.pdf`、`<md5>.jpg`、`<md5>.webp` |
| `byrdocs-bump-test-data` | `metadata.json`、`sitemap.xml`、根目录 schema 文件；`_bump/` 为内部状态 |
| `byrdocs-bump-test-backup` | `<md5>.pdf` 原文件备份 |

混合发布输入是当前大邮条目加三份自建教材、资料、普通试卷样例。样例在 `fixtures.py` 中定义并在输出目录生成，不写入正式 `metadata/`。教材 ISBN 是用于验证格式转换的测试数据，不代表真实出版物。

发布固定使用未修改的 `byrdocs-check v6.5.1`（`bad8331ae6fa49147349b09665b2f1e93ec4277f`），PDFium 使用已固定的 `pypdfium2` 所带库。临时 Git 目录记录输入 YAML 的提交时间，原程序负责文件大小、ISBN、JSON 序列化及 sitemap。它仅访问本地 `127.0.0.1:18765` 的临时上传查询和空 `ids` 发布接口；任何非空发布或其他请求失败。sitemap 使用此测试地址，YAML 下载 URL 仍保持现有格式。首页日期只读查询 `Shxiao111/byrdocs` 的提交。

### 从零配置

1. 注册、验证并登录 Cloudflare，在 R2 页面自行确认结算和开通；无需域名。
2. 创建上述三个 Standard 私有桶，保持 Public Access 为 Disabled。
3. 创建 **Object Read & Write** 令牌，仅授权这三个桶，不授权全部桶或桶管理权限。
4. 在 fork 的 Settings → Secrets and variables → Actions 保存以下设置。

Secrets：`BUMP_TEST_ACCESS_KEY_ID`、`BUMP_TEST_SECRET_ACCESS_KEY`。

Variables：

| 名称 | 值 |
|---|---|
| `BUMP_TEST_ACCOUNT_ID` | Cloudflare Account ID |
| `BUMP_TEST_FILES_BUCKET` | `byrdocs-bump-test-files` |
| `BUMP_TEST_DATA_BUCKET` | `byrdocs-bump-test-data` |
| `BUMP_TEST_BACKUP_BUCKET` | `byrdocs-bump-test-backup` |
| `BUMP_TEST_BACKUP_ENDPOINT` | `https://<Account ID>.r2.cloudflarestorage.com` |
| `BUMP_TEST_BACKUP_REGION` | `auto` |
| `BUMP_TEST_STORAGE_ENABLED` | `true` |
| `BUMP_SCHEDULE_ENABLED` | 配置、演练期间为 `false`；验收完成后设为 `true` |

仅测试配置会映射为原发布程序的环境变量；不要在 fork 填入生产凭据。备份 endpoint 单独配置，但本阶段必须为同一测试账号的 R2 endpoint。

### 第一次运行

在 Actions → **BUMP fork sync** → Run workflow 依次选择 `storage_action`：

1. `probe`：三个桶上传、读回、删除小文件。
2. `exercise`：空环境运行 A→B 替换、上传失败、半完成发布、部分删除、普通/共享文件保护和重复发布；结束后清理演练数据。演练必须暂停每日同步，已有真实 release 时拒绝执行。中断后重跑会依据演练标记及内部清单重置演练，不遍历桶删除未知对象。
3. `publish`：从默认分支的清单重建当前正式版，与已合并 MD5、页码和受管理 YAML 一致才上传。即使版本早已合并也执行，不依赖 RSS 的跳过逻辑。发布大邮条目和三份普通样例，读回确认两个发布文件后清理旧对象。
4. 下载 artifact 抽查 PDF 页数、题目及封面，检查任务日志，再启用每日同步。

实现 PR 合并前可选功能分支运行 `probe`、`exercise`、`publish`；元信息始终从默认分支刷新。`sync` 日常运行使用默认分支；功能分支仅允许同时指定版本和 `retry=true` 的显式重建，先读取默认分支现有元信息，上传成功后将实现和数据一起提交到 fork PR。新增代码必须合并后才能作为每日任务运行。

### 日常更新与恢复

`sync` 先恢复未完成发布和清理，再检查 RSS；新正式版全部重切，上传并读回 PDF、封面、备份后提供 artifact，最后创建 fork PR。版本、`retry` 和待审 PR 规则保持不变。

默认分支变更触发发布；相同清单和元信息的已确认批次不重复覆盖。显式重建同一版本且文件发生变化时分配独立内部批次，旧对象仍等替换 PR 合并、发布确认后才清理。合并与云端发布分别记录。`metadata.json` 和 `sitemap.xml` 分别写入，任何一个失败都禁止清理；原程序可能已写入另一个文件，此时重跑完成发布，不宣称多对象原子更新。

`_bump/state.json` 记录批次、上传前声明的对象所有权、当前/未完成发布和待删除对象；`_bump/confirmed/` 只保存当前已确认的两个发布文件用于读回核对。它们不是客户端读取入口。已有相同内容对象会复用但不自动取得删除权；不同内容则报错。删除只从本任务所有权清单产生，保护仓库全部 YAML、当前已发布条目、待审同步 PR 和固定普通样例。

关闭未合并 PR 的无引用文件在下次每日维护回收；无 PR 的中断批次保留 24 小时。删除失败保留剩余记录；每次重试重新检查引用，新增引用会取消删除。发布失败不删除旧对象，成功后不保留旧 PDF 历史副本。

排错：上传/发布/清理失败先查看具体步骤，再重跑相同 action。原始源文件变化导致 MD5 不一致时不能强制覆盖，需核对源文件和已合并清单。外部修改已发布 JSON 会阻止清理，需先核对状态，不能删除内部日志绕过检查。

停止自动任务：设 `BUMP_SCHEDULE_ENABLED=false`；同时停止手动与合并触发的云端操作则设 `BUMP_TEST_STORAGE_ENABLED=false`。后者保留既有 artifact-only 任务，若要完全暂停同步需两个开关都关闭。Cloudflare R2 Overview 查看用量，Billing 查看实际计费。

测试下载使用 artifact 或 Cloudflare 私有桶的对象下载；当前 `byrdocs.org` URL 不作为测试下载入口。生产 CDN、权限、搜索缓存和其他备份服务商行为仍需正式接入时单独验证。
