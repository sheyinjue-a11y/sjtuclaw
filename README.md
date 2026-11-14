# SJTUClaw

SJTUClaw 是一个面向 Agent Runtime 学习与实践的模块化单体项目。Python 3.12 负责运行时和 FastAPI Gateway，React 工作台由 Gateway 托管；默认使用 DeepSeek，也可在本地工作台切换 OpenAI 兼容服务或 Anthropic。

## 能力概览

- 多轮对话、多 Session、归档/恢复、队列、停止和 steer。
- 长期记忆、上下文预算、自动压缩、可编辑 Soul/System Prompt。
- 附件、PDF/PPT 轻量工作流、工作区、只读/副作用工具和单次审批。
- 一次性或周期定时任务、Skill 快照、手动指定和自动路由。
- 工作台显示附件本轮选择、Skill 调用详情和完整轮次耗时。
- Windows 桌宠 Varka：透明、置顶的 Electron 窗口，右键打开 Quick Chat；它共享浏览器当前 Session。

内置 Skill：`concise`、`course-report`、`pdf`、`presentations`、`paper-mindmap-report-ppt`。

## 架构

```text
React 工作台 / CLI
        -> Gateway / 命令适配层
Scheduler -> RuntimeHost -> AgentLoop -> Provider Runtime -> LLM / ToolRegistry
        -> Session / Memory / Attachment / Workspace / Scheduler / Skill 服务
        -> Ports -> JSON Store、Provider Store、工具与 Token Counter 实现
```

`RuntimeHost` 负责 Session 内串行、跨 Session 并发和 Run 生命周期。Provider Runtime 为每次 Run 固定模型配置，因此切换配置只影响新 Run，不会中断正在执行的请求。

完整目录与模块映射见 [project_structure.md](project_structure.md)。

## 运行环境要求

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11、macOS 或 Linux |
| Python | 3.12 及以上（开发与验收固定 3.12） |
| Node.js | ≥ 20.19（Vite 8 / TypeScript 7 要求，推荐 22 LTS） |
| npm | ≥ 10（随 Node.js 一起安装） |
| 网络 | 可访问 LLM API（默认 DeepSeek） |

- **Windows**：建议使用官方安装器并勾选 “py launcher”，这样 `py -3.12` 可用；脚本执行用 PowerShell。
- **macOS**：`brew install python@3.12`（或 pyenv），终端命令用 `python3.12`。
- **Linux**：Debian/Ubuntu 可用 `sudo apt install python3.12 python3.12-venv`，命令用 `python3.12` 或 `python3`。

## 首次准备

推荐先建虚拟环境；激活后三个平台的命令一致，统一使用 `python`。

**Windows（PowerShell）**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,tokenizer]"
Copy-Item .env.example .env
Set-Location web
npm ci
npm run build
npm run build:desktop
Set-Location ..
python scripts/fetch_deepseek_tokenizer.py
```

**macOS / Linux（bash / zsh）**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,tokenizer]"
cp .env.example .env
cd web && npm ci && npm run build && cd ..
python scripts/fetch_deepseek_tokenizer.py
```

说明：

- `.[dev,tokenizer]` 安装运行、测试/静态检查（dev）和可选精确 Tokenizer（tokenizer）依赖；不需要精确计数时可去掉 `tokenizer`。
- `.env` 只需填写 `LLM_API_KEY`；其余变量（模型、Base URL、上下文预算、压缩阈值等）都带默认值，见 `.env.example`。默认服务是 DeepSeek，也可在工作台“切换模型服务”中保存其他 Provider 配置。不要提交或输出 `.env`、API Key、Cookie。
- 最后一步只为默认 DeepSeek 的精确 Token 统计下载本地 Tokenizer；不下载时在 `.env` 设置 `TOKEN_COUNTER_MODE=approximate`。
- 不建虚拟环境也可以：Windows 用 `py -3.12 -m pip install --user -e ".[dev,tokenizer]"`；macOS/Linux 把 `py -3.12` 换成 `python3.12`。后续命令按平台替换 `python` 为 `py -3.12` 或 `python3.12`。

## 启动

虚拟环境激活后（或已安装到当前 Python）：

```powershell
Set-Location web
npm ci
npm run build
npm run build:desktop
Set-Location ..
py -3.12 -m sjtuclaw.entrypoints.gateway
```

未使用虚拟环境时按平台替换：Windows `py -3.12 -m sjtuclaw.entrypoints.gateway`；macOS/Linux `python3.12 -m sjtuclaw.entrypoints.gateway`。

程序只监听 `127.0.0.1:8765`，并自动打开带一次性本地凭证的工作台。Windows 10/11 上，Electron 和桌宠构建产物齐全时也会同时打开 Varka 桌宠；浏览器与桌宠使用用途隔离、一次性且限时的本地 bootstrap Token。浏览器未打开时，停止后重新执行此命令；不要手工拼接或分享启动链接。

Electron、`web/dist-desktop/` 或 Windows 条件缺失时，Gateway 仅记录警告，浏览器工作台仍可正常使用。桌宠透明点击穿透、始终置顶、多显示器位置与 Electron 崩溃恢复属于 Windows 手工 Demo 验收项，不能由跨平台自动测试完全覆盖。

若 `npm run build:desktop` 在当前网络下载 Electron 时报告 `TypeError: fetch failed`，可仅对本次重建使用镜像：

```powershell
$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
npm run build:desktop
```

仅需终端交互时，可运行：

```powershell
python -m sjtuclaw
```

> **注意：CLI 不支持上传附件。** 验收（case）用例涉及附件上传，请务必在浏览器工作台（Gateway 启动后自动打开）中执行，不要用 CLI。

## 本地命令

CLI 与 Web Composer 共用以下命令；`/exit` 仅 CLI 可用：

```text
/command
/session new|list|switch <id>|rename <id> <title>|delete <id>
/memory list|add <content>|delete <id>
/compact
/skill list|show <skill-name>|usage|refresh
/skill <skill-name> <task>
```

Web 输入 `/` 显示补全；命令结果只作为当前浏览器会话的临时卡片，不写入聊天消息、Run 历史或长期记忆。

## 验证

> **重要：跑验收 case 请使用前端工作台，不要用 CLI。**
> [docs/acceptance.md](docs/acceptance.md) 的验收用例涉及上传附件等操作；CLI 没有上传附件的设计，无法完成附件类 case。请先按上文“启动”运行 Gateway，在自动打开的浏览器工作台中执行验收。
>
> **验收前还需在前端界面手动选择工作区：** 点击“去绑定工作区”并选择项目目录完成绑定；未绑定时写文件等工具会受限，无法正确输出文件。

虚拟环境激活后：

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy src tests
Set-Location web
npm test -- --run
npm run build
npm run build:desktop
Set-Location ..
```

macOS/Linux（bash/zsh）使用：

```bash
cd web && npm test -- --run && npm run build && npm run build:desktop && cd ..
```

未用虚拟环境时，把 `python` 换成 `py -3.12`（Windows）或 `python3.12`（macOS/Linux）。

完整自动检查、人工验收、可选 live 测试和故障排查见 [docs/acceptance.md](docs/acceptance.md)。

## Skill 使用

内置 5 个 Skill：`concise`、`course-report`、`pdf`、`presentations`、`paper-mindmap-report-ppt`。

**命令方式（CLI 与 Web Composer 通用）**

```text
/skill list                 # 列出可用 Skill
/skill show <skill-name>    # 查看某个 Skill 的说明与资源
/skill usage                # 查看调用统计
/skill refresh              # 重新扫描本地 skills/ 目录
/skill <skill-name> <task>  # 使用指定 Skill 完成任务
```

输入 `/` 会显示命令与 Skill 名称补全。

**前端按钮方式（仅 Web 工作台）**

- 输入框附近的“添加”菜单 →“添加 Skill”，选择要启用的 Skill；选中后以 `$skill-name` 形式插入输入框（例如 `$pdf 分析这份报告`）。
- 手动菜单只列出需要显式选择的 Skill；`pdf`、`presentations` 会根据附件自动路由，无需手动选择。
- Skill 调用后，时间线显示 Skill 卡片：调用来源、已读 Skill 资源、失败状态等。

## 前端交互功能

- **会话**：左侧会话列表可新建、切换、归档与恢复；命令结果以临时卡片显示，刷新页面后清除。
- **附件**：“添加”菜单 →“添加文本 / PDF”上传附件，上传后可勾选本轮要使用的附件；附件上传仅在 Web 工作台可用。
- **工作区**：需要工具读写文件时点击“去绑定工作区”，绑定后文件类工具才能工作。
- **审批**：写文件、执行命令等副作用工具按策略需要审批时，工作台弹出审批请求，可允许或拒绝。
- **Soul / 人格**：会话头部“Soul”按钮打开编辑框，修改对话人格与 System Prompt；保存后即时生效，只影响后续 Run。
- **模型服务**：在设置中保存 DeepSeek / OpenAI 兼容 / Anthropic Provider 配置，切换只影响新 Run。
- **定时任务**：定时任务视图创建一次性或周期任务，查看执行记录、归档与恢复。
- **长期记忆**：记忆抽屉查看和管理跨会话记忆条目。
- **轮次详情**：时间线显示每轮耗时、Tool/Skill 调用卡片；Tool 返回下载资源时卡片提供“下载”按钮。

## 安全边界

- Gateway 仅接受 loopback 同源请求；启动凭证只用于建立当前进程的本地 Cookie。
- Provider API Key 只保留在本机；Provider 配置存储受当前 Windows 用户保护，界面和日志不返回 Key。
- Workspace、Skill、Tool 路径校验与 Shell 工作目录都是应用层约束，不等同于 OS Sandbox。
- Skill 不会提升 Tool 权限；写入和执行仍受 `PermissionPolicy` 与审批流程控制。

## 相关文档

- [详细设计文档](docs/design.md)
- [项目结构](project_structure.md)
- [验收指南](docs/acceptance.md)
- [中期报告](docs/midterm-report.md)
- [模型服务设置设计](docs/superpowers/specs/2026-07-25-theme-and-runtime-provider-settings-design.md)
- [PDF/PPT 轻量工作流设计](docs/superpowers/specs/2026-07-29-lightweight-pdf-presentations-design.md)
- [轮次时长与附件选择设计](docs/superpowers/specs/2026-07-29-round-timing-and-attachment-selection-design.md)


## 历史材料与资源归档

提交版代码、亮点文档位于 `archive/submission/`。录屏、加密本地数据的下载与恢复见 [RESOURCES.md](RESOURCES.md)。
