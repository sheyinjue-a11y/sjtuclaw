# 轻量 PDF 与演示文稿能力设计

**日期：** 2026-07-29
**状态：** 待用户审阅
**目标：** 将 Codex 的基础 PDF 与本地 PPTX 工作流迁移到 SJTUClaw，保持现有 Workspace、审批、Shell、下载与 Skill 快照边界；用户不需要手动选择 PDF 或演示文稿 Skill。

## 1. 目标与非目标

### 目标

- 通过内置 `$pdf` Skill 支持 PDF 的检查、简单报告创建、基础表单填写和 PNG 渲染检查。
- 通过内置 `$presentations` Skill 支持基础 PPTX 的创建、内容与画布边界检查，以及可选渲染检查。
- 对含 PDF/PPTX 特征的请求自动附加相应 Skill 快照；网页不再将这两个 Skill 显示为手动添加项。
- 复用现有 `read_skill_resource`、`write_file`、`new_shell`、`run_command`、`create_download` 与审批流程。生成二进制文件仍由 workspace 内经审批执行的脚本产生。
- 所有自动与显式选择均写入既有 `SkillUsage` 审计记录。

### 非目标

- 不迁移 Codex Artifact Tool、Google Slides 路由、Codex Grid 模板库或演示文稿模板编辑。
- 不提供 OCR、复杂图表、复杂动画、PPTX 母版编辑、PowerPoint 在线协作或深度 AcroForm 修复。
- 不新增 Gateway API、持久化格式、独立二进制写入接口或前端生成页面。
- 不将 PPTX 的结构检查误报为视觉检查；没有 LibreOffice 时不会声称完成幻灯片渲染 QA。

## 2. 运行前提与依赖

在现有 `pypdf` 之外，项目运行依赖新增：

- `reportlab`：创建简单 PDF；
- `pdfplumber`：PDF 文本与页面检查；
- `python-pptx`：创建和读取基础 PPTX。

`pdftoppm` 是 PDF 渲染的可选外部命令；`soffice` 是 PPTX 渲染的可选外部命令。两个命令均从 `PATH` 查找，绝不写死 Codex 缓存路径。缺失外部命令不阻止创建和结构检查，但会使相应 `render` 子命令返回稳定的“渲染器不可用”错误。

## 3. Skill 资源

新增两个本地 Skill 包：

```text
skills/
  pdf/
    SKILL.md
    scripts/pdf_workflow.py
    references/input-schema.md
  presentations/
    SKILL.md
    scripts/pptx_workflow.py
    references/input-schema.md
```

`SKILL.md` 说明何时采用脚本、如何读取资源、把脚本和 JSON 规格写入 workspace、申请执行审批、检查输出并创建下载。脚本通过 `read_skill_resource` 按需读取后写入当前 workspace；它们不直接读取 Skill 源目录，也不需要新的 Tool。

### PDF 脚本

`pdf_workflow.py` 提供四个子命令：

- `inspect`：输出页数、提取字符数、加密状态和字段名称；
- `create`：从受限 JSON 生成标题、段落、列表和简单表格 PDF；
- `fill`：按 JSON 中的字段名和值填写已有可填写 PDF，字段不存在时报错；
- `render`：使用 `pdftoppm` 将各页转换成 PNG。

创建 PDF 对中文使用 ReportLab 的 CID 字体。`fill` 只处理标准、已正确挂接的字段，不尝试修复孤立 widget 或移除签名。

### PPTX 脚本

`pptx_workflow.py` 提供三个子命令：

- `create`：从受限 JSON 生成 16:9 标题、正文、图片和简单项目符号幻灯片；
- `inspect`：列出幻灯片文字并检查每个形状是否越过画布；
- `render`：以 `soffice --headless` 转为 PDF，再以 `pdftoppm` 生成 PNG。

默认主题使用简洁高对比的配色与常见 Windows 中文字体。没有 `soffice` 时，`inspect` 仍可完成，但 `render` 不能执行。

## 4. 自动路由与快照选择

自动路由必须位于共享 Runtime 路径，而非单独放在 CLI 或 Gateway：CLI、Gateway 和 Scheduler 由同一规则处理。

- 用户文本包含 `pdf`、`PDF`、`生成 PDF`、`修改 PDF`、`填写 PDF`，或请求附件含 PDF 时，候选为 `pdf`。
- 用户文本包含 `ppt`、`pptx`、`PPT`、`演示文稿`、`幻灯片`、`汇报`，或指向 workspace 中已有的 `.pptx` 文件时，候选为 `presentations`。当前附件系统只接受文本和 PDF，不扩展为通用 PPTX 上传。
- 两类特征同时出现时，按 `pdf`、`presentations` 顺序附加两个快照。
- 用户显式 Skill 快照优先保留；自动候选只补足空位，单次 Run 最多三个 Skill。无空位时记录安全的进度事件并继续执行，不使请求失败。
- 自动候选与显式候选重复时去重。Skill 不可用时跳过，并记录安全原因。
- 已自动加入的快照按现有 `SkillUsageSource.AUTO` 与原因（例如 `automatic PDF routing`）保存。后续读取资源仍仅允许使用该 Run 固定的快照。

网页 Skill 添加菜单过滤 `pdf` 和 `presentations`，避免用户必须手选；现有命令和 `$skill` mention 保持兼容，不作为推荐操作。

## 5. 数据流与安全边界

1. Runtime 接收请求、文本和附件引用。
2. 共享路由依据文本和已就绪附件元数据选择 PDF/PPTX 快照，并与已有显式快照合并。
3. `ContextBuilder` 将已选择 Skill 的正文注入初始模型上下文，使模型第一步即可读取脚本资源。
4. 模型通过既有 Tool 将脚本与输入 JSON 写入绑定 workspace，并申请 Shell/文件写入审批。
5. 经批准后在 workspace 中执行脚本，检查生成文件；如有渲染器，再生成并检查 PNG。
6. 模型调用既有下载 Tool 发布最终 PDF/PPTX 的不可变下载快照。

所有路径均使用当前 workspace 内相对路径。脚本只接收显式输入和输出路径，不读取环境机密；写入与命令执行继续受现有 PermissionPolicy 和审批控制。

## 6. 错误策略

脚本应返回稳定、无敏感路径的错误类别：

- `ARTIFACT_INPUT_INVALID`：JSON、参数或相对路径无效；
- `ARTIFACT_DEPENDENCY_MISSING`：Python 包不可用；
- `PDF_FIELD_NOT_FOUND`：基础表单字段不存在；
- `PDF_RENDERER_UNAVAILABLE`：找不到 `pdftoppm`；
- `PRESENTATION_RENDERER_UNAVAILABLE`：找不到 `soffice`；
- `PRESENTATION_BOUNDS_ERROR`：元素越出幻灯片画布；
- `ARTIFACT_OUTPUT_FAILED`：输出无法创建或读取。

若创建已成功但视觉渲染器缺失，Skill 必须报告“仅完成结构检查，尚未完成视觉验证”，并保留生成文件供用户下载或本机复核。

## 7. 实施与测试

### 代码和文档改动

- 更新 `pyproject.toml` 依赖；
- 新增自动路由纯函数/服务，并在 Runtime 共享请求路径合并 Skill 快照；
- 调整网页 Skill 菜单过滤默认 Skill；
- 新增两个 Skill 包与可读脚本资源；
- 更新 `paper-mindmap-report-ppt` 以引用本地 `$pdf` 与 `$presentations`；
- 更新 README 与 `project_structure.md`。

### 自动测试

- 自动路由：文本、PDF 附件、workspace PPTX 路径、去重、显式优先、三项上限、不可用 Skill；
- PDF：创建、检查、基础字段填写，以及缺少 `pdftoppm` 的稳定错误；
- PPTX：创建、检查、越界检测，以及缺少 `soffice` 的稳定错误；
- Skill registry：两个新增 Skill 和全部文本资源可扫描；
- Web：默认 Skill 不出现在手动添加菜单。

运行已有 Python 测试、Ruff、Mypy 与 Web 测试/构建。若本机具备外部渲染器，再执行真实 PDF/PPTX 渲染冒烟检查；没有时明确记录为未覆盖的可选视觉验证。

## 8. 验收标准

- `/skill list` 显示可用的 `$pdf` 和 `$presentations`。
- 用户直接提出 PDF 或 PPTX 任务，无需 `$skill`，初始 Run 即带有匹配的自动 Skill 快照。
- 用户在绑定 workspace 后可经审批生成、检查并下载简单 PDF 与 PPTX。
- PDF 可在 `pdftoppm` 存在时渲染为 PNG；PPTX 可在 `soffice` 和 `pdftoppm` 存在时渲染为 PNG。
- 缺少渲染器仅产生稳定提示，不阻断结构检查或误报视觉 QA 完成。
- 其它 Skill 的显式使用、现有附件、审批、下载和运行历史行为不回归。
