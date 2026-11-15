# SJTUClaw 项目结构

本文记录当前源码目录、职责映射和依赖方向。运行产生的 `.env`、`.sjtuclaw/`、`build/`、缓存、`web/dist/` 和 `web/dist-desktop/` 不属于业务源码。

## 1. 顶层目录

```text
new_computer_programming_practice/
├── src/sjtuclaw/          # Python 核心应用
├── web/                   # React 工作台
├── skills/                # 本地内置 Skill
├── tests/                 # 单元、契约、集成与 live 测试
├── docs/                  # 面向使用者文档、设计与历史计划
├── scripts/               # Tokenizer 等辅助脚本
├── vendor/                # 可选的本地 DeepSeek Tokenizer
├── .env.example           # 环境变量示例
├── pyproject.toml         # Python 包、依赖与检查配置
├── README.md              # 使用说明
├── project_structure.md   # 本文
└── SJTUClaw.md            # 课程实验要求
```

## 2. Python 核心结构

```text
src/sjtuclaw/
├── __main__.py
├── bootstrap.py           # 组合根：装配运行时与基础设施
├── config.py              # 环境配置、Provider 与模型配置类型
├── errors.py
├── entrypoints/
│   ├── cli.py
│   ├── gateway.py         # REST、SSE、同源认证、静态工作台和桌宠启动
│   ├── gateway_auth.py    # 按 browser/pet 用途签发、一次性消费 bootstrap Token
│   ├── pet_launcher.py    # Windows Electron 桌宠发现、启动与一次崩溃恢复
│   └── gateway_streams.py
├── runtime/
│   ├── agent_loop.py
│   ├── active_session.py  # 浏览器与桌宠共享的当前 Session 选择和回退规则
│   ├── service.py
│   ├── archive_service.py
│   ├── attachment_service.py
│   ├── commands.py
│   ├── compaction.py
│   ├── context_builder.py
│   ├── memory_*.py
│   ├── permission.py
│   ├── prompt_configuration.py
│   ├── provider_runtime.py
│   ├── run_control.py
│   ├── scheduler.py
│   ├── session_*.py
│   ├── skill_routing.py
│   ├── skill_service.py
│   ├── token_usage.py
│   └── workspace_service.py
├── models/                # Session、Message、Run、Memory、Task、Skill 等模型
├── ports/                 # LLM、Store、Tool、Scheduler、Skill 等抽象边界
├── infrastructure/
│   ├── llm/
│   │   ├── deepseek.py
│   │   ├── openai_compatible.py
│   │   ├── anthropic.py
│   │   └── tool_protocol.py
│   ├── persistence/
│   │   ├── json_store.py
│   │   ├── in_memory.py
│   │   ├── attachments.py
│   │   ├── memory_policy.py
│   │   ├── provider_settings.py
│   │   ├── scheduler.py
│   │   └── skills.py
│   ├── tokenizers/        # DeepSeek 精确与近似计数器
│   ├── tools/             # 文件、附件、Shell、下载、Skill 工具
│   └── runtime.py         # 时钟与 UUID 实现
├── prompts/               # System Prompt、Soul、压缩与记忆规则
└── skills/registry.py     # Skill 清单解析与资源规则
```

`memory_*.py` 和 `session_*.py` 是同一职责族的多份实现文件；实际文件列表以源码为准，本文不把运行时生成文件混入树中。

## 3. 模块职责

### 3.1 入口与工作台

- `__main__.py`、`entrypoints/cli.py`：CLI 交互和 `/exit`。
- `entrypoints/gateway.py`：本地 REST/SSE、一次性 bootstrap、Cookie、附件、会话、任务、Memory、Prompt、Provider 和静态工作台；就绪后同时尝试启动 Windows 桌宠。
- `entrypoints/gateway_auth.py`：按 `browser`、`pet` 目的签发、限时且一次性消费的 bootstrap Token。
- `entrypoints/pet_launcher.py`：仅在 Windows 且 Electron 与桌宠构建产物齐全时启动 Electron；异常退出只重启一次，不影响 Gateway/浏览器工作台。
- `entrypoints/gateway_streams.py`：将 Runtime 事件映射为公开 SSE 事件，包含 Tool 与 Skill 选择状态。
- `web/src/App.tsx`：聊天、会话归档、Provider 设置、附件与本轮选择、工作区、审批、Skill 详情和轮次耗时。
- `web/src/SchedulerView.tsx`：定时任务创建、编辑、归档、恢复和执行记录。

### 3.2 Agent Runtime、会话与上下文

- `runtime/service.py` 与 `agent_loop.py`：请求队列、Session 内串行、跨 Session 并发、Run 停止/恢复、模型调用和 Tool 闭环；归档时会取消残留排队 Run。
- `context_builder.py`、`compaction.py`、`token_usage.py`：构造模型上下文、压缩长对话和统计上下文/轮次用量。
- `session_service.py`、`session_coordinator.py`、`session_state.py`、`session_title.py`、`archive_service.py`：会话生命周期、协调、状态、标题和归档。
- `active_session.py`：保存浏览器选择的当前 Session；选择失效时回退到最近未归档 Session，均不存在时创建新 Session。
- `commands.py`：CLI 与 Gateway 共用的本地命令解析和执行。

### 3.3 Provider、Prompt 与长期记忆

- `provider_runtime.py`：为完整 Run 租用固定 Provider 客户端；切换后只影响新 Run。
- `infrastructure/llm/`：DeepSeek、OpenAI 兼容和 Anthropic 协议适配。
- `infrastructure/persistence/provider_settings.py`：在本机保存受当前 Windows 用户保护的 Provider 配置。
- `prompt_configuration.py` 与 `prompts/`：可热更新的 Soul/System Prompt，以及打包的默认提示词。
- `memory_service.py`、`memory_retriever.py`、`memory_extractor.py`、`memory_capture.py`、`memory_policy.py`：跨 Session 记忆 CRUD、检索、静默提取与规则管理。

### 3.4 工具、附件、工作区与审批

- `attachment_service.py` 与 `infrastructure/persistence/attachments.py`：上传、绑定、读取和删除附件。
- `workspace_service.py`：Session 工作区绑定。
- `infrastructure/tools/readonly.py`、`advanced.py`、`shells.py`、`downloads.py`：文件/目录读取，写入、补丁、附件复制、Shell 与下载。
- `permission.py`：`allow`、`ask`、`deny` 决策；副作用通过审批继续或拒绝。

### 3.5 定时任务与 Skill

- `scheduler.py` 和 `infrastructure/persistence/scheduler.py`：一次性/周期任务、重试、执行历史与归档。
- `skill_service.py`、`skill_routing.py`、`skills/registry.py`：清单扫描、快照、显式指定、自动路由和使用记录。
- `infrastructure/tools/skill_selection.py`、`skills.py`：模型选择 Skill、读取 Skill 资源。

顶层 `skills/` 当前有 5 个内置 Skill：

```text
skills/
├── concise/                       # 简洁输出
├── course-report/                 # 课程报告 Markdown
├── pdf/                           # PDF 创建、填写、渲染与检查
├── presentations/                 # 轻量 PPT 创建与检查
└── paper-mindmap-report-ppt/      # 论文 + 思维导图学术汇报 PPT
```

## 4. 前端与测试

```text
web/
├── index.html
├── pet.html                 # Electron 桌宠渲染页
├── package.json / package-lock.json
├── node_modules/            # 由 npm ci 生成，不属于业务源码
├── desktop/                 # Electron 主进程、preload 与窗口位置/侧栏方向逻辑
├── public/pets/varka/       # 固定 Varka v2 atlas（pet.json、spritesheet.webp）
├── vite.config.ts
├── tsconfig.json / tsconfig.app.json / tsconfig.node.json
├── src/
│   ├── App.tsx
│   ├── SchedulerView.tsx
│   ├── MemoryDrawer.tsx
│   ├── commands.ts / commands.test.ts
│   ├── commandTimeline.ts / commandTimeline.test.ts
│   ├── roundTiming.ts / roundTiming.test.ts
│   ├── skillMenu.ts / skillMenu.test.ts
│   ├── toolResources.ts / toolResources.test.ts
│   ├── memory.ts
│   ├── main.tsx
│   ├── pet/                 # Quick Chat、SSE 状态映射和桌宠前端 API
│   └── styles.css
├── dist/                    # 浏览器工作台构建产物，由 npm run build 生成
└── dist-desktop/            # Electron 构建产物，由 npm run build:desktop 生成

tests/
├── unit/          # Runtime、Provider、PDF/PPT、工具与组件逻辑
├── contract/      # JSON/内存 Store 契约
├── integration/   # Gateway、队列、归档、任务与安全边界
├── live/          # 可选真实 DeepSeek API 验证
└── fixtures/      # 隔离测试输入
```

`web/src/toolResources.ts`：验证并规范化 Host 可渲染的 Tool 资源。
`web/src/commandTimeline.ts`：浏览器内存中的斜杠命令与回执时间线状态。

## 5. 依赖方向

```text
React 工作台 --选择当前 Session--> Gateway -> ActiveSessionCoordinator -> Runtime 服务 -> Models + Ports
Electron 桌宠 --Quick Chat--------^                    ^
CLI ------------------------------^                    ^
Scheduler ----------------------------------|
Infrastructure（LLM、Store、Token Counter、Tool）实现 Ports
bootstrap.py 在组合根选择并连接具体实现
Gateway -> PetLauncher -> Electron 主进程 -> preload -> web/src/pet/ -> Gateway
```

- `models` 不依赖 Gateway、React 或具体 Provider。
- `runtime` 通过 `ports` 使用外部能力，业务规则不写入入口层。
- `infrastructure` 提供 Provider、持久化和工具实现；不会决定会话或审批规则。
- Gateway 只监听 `127.0.0.1`，构建后的 `web/dist/` 存在时由同一进程托管。
- 桌宠仅在 Windows 使用 `web/dist-desktop/`、Electron 和 `web/public/pets/varka/`；桌宠 bootstrap Token 与浏览器 Token 按用途隔离，并经 HttpOnly 客户端 Cookie 建立会话。

启动、构建和验收命令见 [README.md](README.md) 与 [docs/acceptance.md](docs/acceptance.md)。


## 2026-11-14 归档补充

- `archive/submission/`：原提交压缩包内代码与说明文档。
- `vendor/`：原有 tokenizer 文件（可选离线资源）。
- `scripts/restore_resources.py`：按资源清单恢复 Release 附件。
- `RESOURCES.md`、`resources-manifest.json`、`MIGRATION_FILES.json`：资源入口、恢复映射与校验信息。
