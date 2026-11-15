# SJTUClaw Step 0–5 实施计划

**规格来源：** `docs/superpowers/specs/2026-07-15-sjtuclaw-core-design.md`

**总目标：** 完成可真实使用、可验证、可回滚的 SJTUClaw Step 0–5，同时保持 Step 6–9 的统一 Runtime 扩展边界。

**架构摘要：** 使用 Python 3.12 模块化单体。CLI 通过统一 RuntimeHost 提交请求；Runtime 依赖 LLM、Store、TokenCounter 和 Tool Ports；DeepSeek、JSON、Tokenizer 与只读 Tool 位于基础设施层。

**全局约束：** 只实现 Step 0–5；Step 6–9 仅保留已批准合同，Step 10 仅保留迁移设计。真实 DeepSeek 与 CLI 验收由实施代理完成，UI/Gateway 未来由用户验收。`.env` 和现有用户工作树状态不得提交或泄漏。行为变化必须同步更新 living spec。

## 目标 1：建立可运行、可检查的工程基础

**规格依据：** §3 技术基线与配置、§4 架构、§6 核心模型与组件、§11 错误模型、§15 Living Spec 与 Git 提交。

**目标结果：** 项目成为可安装、可测试的 Python 3.12 包，并具备配置、错误、核心模型与 Ports 的稳定基础。

**范围：** 建立已批准目录边界、开发依赖、独立 System Prompt/Soul 配置、Secret 安全加载和 FakeClock/FakeIdGenerator 等测试基础。不创建 Gateway、Scheduler、Approval UI、Skill 或 SQLite 空实现。

**依赖：** 已批准设计规格。

**验收标准：** 全局 Python 3.12 环境可完成 editable 安装；配置缺失和非法值产生明确错误；API key 不出现在 repr、日志或测试输出；基础模型、配置、lint 和类型检查通过。

**Git 检查点：** `chore: scaffold Python project and core contracts`

## 目标 2：打通真实 DeepSeek 多轮对话与统一 Runtime

**规格依据：** §1 目标、§5 统一调用与 Session 并发、§6.2 核心组件、§9.1 LLM Adapter、§11 错误模型、§12.2 真实 DeepSeek 强制验收。

**目标结果：** CLI 能通过 OpenAI-compatible Chat Completions 与真实 DeepSeek 对话，AgentRuntime 使用 submit/RunHandle/RuntimeEvent 合同运行多轮会话。

**范围：** 完成 DeepSeek LLM Adapter、usage 映射、临时错误重试、基础 ContextBuilder、CLI 输入输出和 Ctrl+C；先支持文本响应，不提前实现 Tool。RuntimeHost 从一开始保留按 Session 排队和服务端序号能力。

**依赖：** 目标 1 的配置、模型和 LLM Port。

**验收标准：** Fake LLM 的确定性多轮测试通过；真实 DeepSeek 基础调用和两轮记忆测试通过；`usage.prompt_tokens` 可观测；同一 Session 的请求保持完整 Turn 顺序；错误被转换为统一 RuntimeEvent。

**Git 检查点：** `feat: add LLM client and multi-turn runtime`

## 目标 3：完成持久 Session、稳定上下文与 CLI 管理

**规格依据：** §5.2 CLI 与 Gateway 共享 Session、§6.1 持久领域模型、§7 JSON 持久化、§8 Context 与 Compaction 中的稳定上下文边界、§12 测试与验收。

**目标结果：** 多 Session、Memory、System Prompt 和 Soul 可持久、可恢复，并通过同一个 ContextBuilder 进入模型输入。

**范围：** 实现每 Session 一文件及 `memory.json`、原子替换、schemaVersion、Store 契约、全部 Session/Memory CLI 命令和重启恢复。CLI/Gateway 兼容性落实为显式 session_id、服务端排序和单 RuntimeHost 写入规则，但不实现 Gateway。

**依赖：** 目标 2 的 Runtime 与消息模型。

**验收标准：** Session 创建、列出、切换、重命名、删除与重启恢复均通过 CLI 验证；Memory 跨 Session 生效；损坏文件不会被静默覆盖；同 Session 并发提交严格串行，不同 Session 可独立运行；只有 Store 进行 JSON I/O。

**Git 检查点：** `feat: add persistent sessions and stable context`

## 目标 4：完成 Token 预算与 Compaction

**规格依据：** §8 Context 与 Compaction、§12.1 确定性测试、§12.2 真实 DeepSeek 强制验收、§17 已知风险。

**目标结果：** 长对话按批准的 Token 预算自动压缩，`/compact` 可手动触发，稳定上下文和完整 Agent Turn 始终受保护。

**范围：** 先用 DeepSeek 官方资料和最小实验确认离线 Tokenizer 集成，再实现 DeepSeek TokenCounter 与显式 approximate 模式；完成触发、目标、近期 Turn 保留、摘要生成和预算失败处理。

**依赖：** 目标 3 的持久 Session、ContextBuilder 和真实 LLM。

**验收标准：** 确定性 TokenCounter 能覆盖所有阈值与边界；ToolCall/ToolResult 预留结构不会被拆分；System Prompt、Soul、Memory、Tool Definitions 不被改写；真实 DeepSeek `/compact` 生成可恢复使用的摘要；Tokenizer 失败不会静默降级。

**Git 检查点：** `feat: add token-based compaction`

## 目标 5：完成只读 Tool、PermissionPolicy 与 Agent Loop

**规格依据：** §9 LLM 与 Tool 协议、§10 Agent Loop 与 Permission、§11 错误模型、§12 测试与验收、§13 Step 6–9 兼容边界。

**目标结果：** 模型能通过 native 或严格 JSON 协议调用只读 Tool，Runtime 在 PermissionPolicy 后执行，并把 ToolResult 安全反馈给下一轮模型。

**范围：** 实现 current_time、list_dir、read_file、ToolRegistry、ToolIntent/AuthorizedToolCall、allow/deny、批次验证、Tool trace、循环上限和重复调用阻断。保留 ask/PendingApproval 的 Step 8 合同，但不实现 Approval 交互。

**依赖：** 目标 4 的完整 Context 与持久消息语义。

**验收标准：** native 和 JSON 模式的 Fake/真实 DeepSeek 闭环均通过；malformed 或 schema 非法批次零执行；deny 无法绕过；`.env`、`.git`、越界路径不可读；第六次连续同签名调用被阻断；最大 Step、Tool 错误和中断均产生正确记录且不破坏 Session。

**Git 检查点：** `feat: add tool loop and permission policy`

## 目标 6：完成真实验收、文档与交付闭环

**规格依据：** §12 测试与验收、§15 Living Spec 与 Git 提交、§16 明确偏离课程最低要求的安全决策、§17 已知风险。

**目标结果：** Step 0–5 的自动测试、真实 DeepSeek、CLI 手工流程、静态检查和使用文档形成可重复的交付证据。

**范围：** 汇总离线、Store 契约、Runtime 集成和 live 测试；由实施代理完成 CLI 的 Session、Memory、Compaction、native/json Tool 与重启恢复验收；记录操作、预期结果和故障排查。UI/Gateway 明确保持为未来用户验收项。

**依赖：** 目标 1–5 全部完成。

**验收标准：** 全部离线与 live 测试、ruff、mypy 通过；真实 DeepSeek 覆盖基础对话、多轮、两种 Tool 模式、ToolResult 回环、Compaction 与安全 API 错误映射；CLI 验收使用隔离数据目录且不污染正式 Session；Git 检查确认 `.env`、`SJTUClaw.md`、`instruction_doc.md` 删除和 `.superpowers/` 均未误提交。

**Git 检查点：** `test: complete live DeepSeek and CLI acceptance`

## 规格覆盖检查

- Step 0–1：目标 1–2。
- Step 2–3：目标 3。
- Step 4：目标 4。
- Step 5：目标 5。
- 跨阶段真实验证与交付：目标 6。
- Step 6–9 兼容边界：目标 2、3、5 中建立合同，不实现功能。
- Step 10：仅由 spec 保留迁移设计，不进入实施目标。
