# SJTUClaw 验收指南

本指南覆盖当前 CLI、Gateway、React 工作台、Provider、长期记忆、附件、工作区、审批、定时任务和 Skill 能力。历史 Step 文档只保留实现过程，不再作为当前验收范围。

## 1. 准备

首次使用：复制 `.env.example` 为 `.env`，仅在本地填写 `LLM_API_KEY`；安装 Python 依赖并构建前端。默认 DeepSeek 精确 Token 统计还需下载 Tokenizer；不下载时设置 `TOKEN_COUNTER_MODE=approximate`。

```powershell
py -3.12 -m pip install --user -e ".[dev,tokenizer]"
Copy-Item .env.example .env
Set-Location web; npm ci; npm run build; Set-Location ..
py -3.12 scripts/fetch_deepseek_tokenizer.py  # 仅默认 DeepSeek 精确计数需要
```

不得提交、复制到终端输出或截图分享 `.env`、API Key、Cookie 和含 bootstrap token 的启动链接。

## 2. 自动检查

在项目根目录执行：

```powershell
py -3.12 -m pytest -q
py -3.12 -m ruff check .
py -3.12 -m mypy src tests
py -3.12 -m compileall -q src
Set-Location web; npm test -- --run; npm run build; Set-Location ..
```

预期：全部命令退出码为 `0`。Python 测试覆盖 Store 契约、Runtime、Gateway、Provider 适配、PDF/PPT 工作流和安全边界；Vitest 覆盖前端纯逻辑；`npm run build` 验证 TypeScript 与生产静态包。

可选真实 DeepSeek 验收会消耗本地 API 配额：

```powershell
$env:RUN_LIVE_DEEPSEEK = '1'
py -3.12 -m pytest -q -m live
```

## 3. 工作台人工验收

启动只需：

```powershell
py -3.12 -m sjtuclaw.entrypoints.gateway
```

预期：进程只监听 `127.0.0.1:8765`，浏览器自动打开当前进程的一次性本地启动页。浏览器没有打开时，停止进程后重新运行命令；不要直接访问或分享手工拼出的启动 URL。

在已配置可用模型服务的工作台中检查：

1. 发送一条普通消息；能看到流式内容、最终回复和 Session 标题。耗时至少 1 秒的完成回复上方显示“已处理 …”。
2. 新建、重命名、归档并恢复一个 Session；归档后页面不可继续修改，恢复后仍可查看历史。
3. 上传一个 PDF 或普通文件，选择两个附件后点击“取消本轮选择”；选择标记和编辑区标签清空，文件仍保留。发送带 PDF 的任务时，确认 Skill 区显示显式或自动选择及已读资源。
4. 在“切换模型服务”中查看 Provider、模型、Base URL 与思考模式；保存后新请求使用新配置，正在执行的请求不被切换打断。界面不显示 API Key。
5. 打开“记忆”，添加、编辑和删除测试记忆；编辑冲突时刷新后重试。修改 Soul/System Prompt 后发送新请求，确认只影响后续请求。
6. 绑定一个测试工作区，发起需要写入或 Shell 的操作；确认审批出现，拒绝不执行，允许后才继续。测试目录仅使用无敏感内容的临时目录。
7. 新建一次性或周期定时任务，检查执行记录；归档、恢复和永久删除仅作用于测试任务。

## 4. CLI 验收

使用隔离数据目录，避免把验收数据写入日常数据：

```powershell
$env:SJTUCLAW_DATA_DIR = 'build/cli-acceptance'
$env:TOOL_ROOT = 'tests/fixtures'
$env:TOKEN_COUNTER_MODE = 'approximate'
py -3.12 -m sjtuclaw
```

输入以下命令：

```text
/command
/session new
/session list
/memory add CLI_ACCEPTANCE_MEMORY_42E7
/memory list
/skill list
/skill show pdf
/exit
```

预期：命令均返回对应结果；新 Session 出现在列表；`CLI_ACCEPTANCE_MEMORY_42E7` 可跨 Session 查询；`pdf` 的版本和描述可显示。需要验证 Tool 闭环时，可在同一隔离会话中要求模型读取 `cli_tool_fixture.txt`，并确认仅在 `TOOL_ROOT` 内读取。

## 5. PDF 与 PPT Skill 验收

在绑定测试工作区后，上传一份状态为「可用」的 PDF，不在输入中手动添加 `$skill`，发送：

```text
根据这份 PDF 生成 5 页 PPT 汇报并导出
```

预期：本轮 Run 的 Skill 区显示 `pdf` 与 `presentations`，选择原因是自动路由；两者都出现在本轮资源读取记录中。`pdf` 和 `presentations` 不出现在「Add Skill」手动选择菜单中，因为它们始终由运行时按任务和附件自动选择。

也可在项目根目录直接做离线冒烟检查：

```powershell
New-Item -ItemType Directory -Force build/artifact-acceptance | Out-Null
@'
{"title":"PDF smoke","sections":[{"heading":"Result","paragraphs":["created locally"]}]}
'@ | Set-Content -Encoding utf8 build/artifact-acceptance/pdf.json
py -3.12 skills/pdf/scripts/pdf_workflow.py create --spec build/artifact-acceptance/pdf.json --output build/artifact-acceptance/report.pdf
py -3.12 skills/pdf/scripts/pdf_workflow.py inspect --input build/artifact-acceptance/report.pdf

@'
{"title":"PPT smoke","slides":[{"title":"Result","bullets":["created locally"]}]}
'@ | Set-Content -Encoding utf8 build/artifact-acceptance/pptx.json
py -3.12 skills/presentations/scripts/pptx_workflow.py create --spec build/artifact-acceptance/pptx.json --output build/artifact-acceptance/report.pptx
py -3.12 skills/presentations/scripts/pptx_workflow.py inspect --input build/artifact-acceptance/report.pptx
```

若安装了 Poppler，可继续执行 PDF 渲染并目视检查生成的 PNG；若安装了 LibreOffice 和 Poppler，可继续执行 PPTX 渲染。缺少可执行渲染器时，命令应安全地返回对应的 `*_RENDERER_UNAVAILABLE`，而不是写入工作区以外的位置。

```powershell
py -3.12 skills/pdf/scripts/pdf_workflow.py render --input build/artifact-acceptance/report.pdf --output-dir build/artifact-acceptance/pdf-pages
py -3.12 skills/presentations/scripts/pptx_workflow.py render --input build/artifact-acceptance/report.pptx --output-dir build/artifact-acceptance/pptx-pages
```

## 6. 故障排查

| 现象 | 排查 |
| --- | --- |
| `Configuration error: LLM_API_KEY is required` | 在本地 `.env` 填写 Key；不要把 Key 贴入终端或提交 Git。 |
| 浏览器未自动打开或 API 返回未认证 | 保持终端运行，停止后重新执行启动命令，生成新的单次 bootstrap。 |
| 页面只有 API、没有工作台 | 在 `web/` 执行 `npm ci`、`npm run build`，确认生成 `web/dist/` 后重启 Gateway。 |
| Tokenizer 初始化失败 | 运行下载脚本，或在 `.env` 设置 `TOKEN_COUNTER_MODE=approximate`。 |
| 切换 Provider 后请求失败 | 检查 Provider、模型、Base URL 与本地 Key 是否配套；修正后再次保存，旧 Run 不会自动重放。 |
| 附件无法发送 | 等待附件就绪；取消本轮选择只取消引用，不删除文件。检查单文件大小未超过 `TOOL_MAX_FILE_BYTES`。 |
| `429`、网络或 `5xx` | 已接受的用户消息会保留；稍后在同一 Session 重试。不要重复提交可能触发副作用的请求。 |
| Tool 被拒绝 | 检查 `TOOL_ROOT`、Workspace、审批决定和路径；`.env`、`.git`、越界路径按设计拒绝。 |
| 记忆无法读取或保存 | 先备份 `.sjtuclaw/memory.json`，修复 JSON 或从可靠备份恢复，再在工作台重试。 |

## 7. 安全与未覆盖范围

- `PermissionPolicy`、Workspace、Skill 和 Tool 路径检查是应用层边界，不是 OS Sandbox；Shell 当前目录也不是隔离环境。
- Gateway 只接收 loopback 同源请求。Provider Key 的本地持久化受当前 Windows 用户保护，但仍应视为敏感数据。
- 自动测试不替代浏览器的视觉、响应式和无障碍检查，也不替代真实外部模型、网络、Provider 账单或校园系统接入验证。
