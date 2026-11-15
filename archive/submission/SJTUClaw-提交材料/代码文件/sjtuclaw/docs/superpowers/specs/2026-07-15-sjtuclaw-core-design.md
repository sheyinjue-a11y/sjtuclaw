# SJTUClaw Step 0–5 核心设计与 Step 6–10 演进边界

- 状态：已批准，作为后续实施的 living spec
- 日期：2026-07-15
- 当前实施范围：Step 0–5
- 兼容架构范围：Step 6–9
- 未来迁移范围：Step 10 SQLite

## 1. 目标

本轮实现一个 Python 3.12 的模块化单体 Agent Runtime，完成课程参考文档 Step 0–5：

1. 连接 OpenAI-compatible Chat Completions API。
2. 支持 CLI 多轮对话。
3. 支持多 Session 与 JSON 持久化。
4. 支持 System Prompt、Soul 与跨 Session Memory。
5. 支持基于 Token 预算的 Compaction。
6. 支持只读 Tool、Tool Result 反馈和安全 Agent Loop。

核心模块必须可替换、带类型标注，并由 pytest、Store 契约测试、真实 DeepSeek 测试和 CLI 验收覆盖。

同时定义 Step 6–9 所需的稳定边界，使未来 Gateway、Scheduler、Workspace、Approval 和 Skill 能复用同一个 Runtime，不复制 Session、Context、Compaction 或 Agent Loop。

## 2. 非目标

本轮不实现：

- Gateway、WebSocket、HTTP API、GUI 或附件上传；
- Scheduler 或定时任务执行器；
- 写文件、改文件、删除文件、Shell 等有副作用 Tool；
- Approval 交互与暂停恢复；
- Skill 加载与执行；
- SQLiteStore、JSON-to-SQLite 迁移或双写；
- LangChain 等隐藏课程核心概念的 Agent 框架；
- 多进程直接并发修改同一 JSON 数据目录。

Step 6–9 仅定义兼容边界。Step 10 仅定义未来迁移方案。

## 3. 技术基线与配置

- Python 3.12；
- 所有核心接口和模型使用类型标注；
- pytest 负责测试，ruff 负责静态风格检查，mypy 负责类型检查；
- LLM 使用 OpenAI-compatible Chat Completions；
- API key 只从本地 `.env` 或进程环境读取，不写入仓库、日志、异常文本或测试快照。

已确定的环境变量：

```text
LLM_API_KEY=<local secret>
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
LLM_TOOL_MODE=native|json
SJTUCLAW_DATA_DIR=.sjtuclaw

AGENT_MAX_STEPS=20
AGENT_REPEAT_CALL_LIMIT=5

CONTEXT_BUDGET_TOKENS=65536
COMPACTION_TRIGGER_RATIO=0.80
COMPACTION_TARGET_RATIO=0.50
COMPACTION_KEEP_RECENT_TURNS=4
LLM_OUTPUT_RESERVE_TOKENS=8192

TOKEN_COUNTER_MODE=deepseek|approximate
```

`AGENT_MAX_STEPS=0` 表示不限制总 Step。`TOKEN_COUNTER_MODE=approximate` 必须由用户显式选择；官方 Tokenizer 初始化失败时不能静默降级。`LLM_TOOL_MODE` 也不允许运行时自动切换。

## 4. 架构

采用模块化单体与 Ports/Adapters 思路，但使用更直白的目录命名：

```text
src/
  sjtuclaw/
    models/
      session.py
      message.py
      tools.py
      runtime.py
      approval.py

    runtime/
      service.py
      session_coordinator.py
      context_builder.py
      compaction.py
      agent_loop.py
      permission_policy.py

    ports/
      llm.py
      stores.py
      token_counter.py
      tools.py
      approval.py
      skills.py

    entrypoints/
      cli.py

    prompts/
      system_prompt.md
      soul.md

    infrastructure/
      llm/
        deepseek.py
      persistence/
        json_store.py
      tokenizers/
        deepseek.py
        approximate.py
      tools/
        readonly.py

    config.py
    bootstrap.py
    errors.py
    __main__.py

tests/
  unit/
  contract/
  integration/
  live/
```

目录表达职责，不要求为未来功能创建空文件或空目录。`approval.py`、`skills.py` 等未来 Port 只有在当前兼容合同实际需要类型时才创建；不得制造无行为的占位实现。

### 4.1 依赖方向

```text
CLI / Future Gateway / Future Scheduler
                  |
                  v
          AgentRuntime / RuntimeHost
                  |
      Session + Context + Agent Loop
                  |
                  v
       LLM / Store / Token / Tool Ports
                  |
                  v
      DeepSeek / JSON / Tokenizer / Tools
```

- `models` 不依赖 CLI、DeepSeek、JSON 或 HTTP。
- `runtime` 只依赖 `models` 和 `ports`。
- `entrypoints` 把外部输入转换为统一请求，并消费统一事件。
- `infrastructure` 实现外部 API、文件和 Tool 细节。
- `bootstrap.py` 是组合根，负责把 Runtime Ports 接到具体 infrastructure；入口不自行组装这些依赖。
- 入口不能直接访问 LLM、Store 或 ToolExecutor。

## 5. 统一调用与 Session 并发

### 5.1 应用接口

统一入口采用提交与事件句柄，而不是让每个入口自己维护循环：

```python
handle = await runtime.submit(request)

async for event in handle.events():
    ...
```

`RuntimeRequest` 至少表达：

```text
request_id
session_id
source             # CLI | GATEWAY | SCHEDULER
user_input
actor_id?          # Step 6 以后使用
attachment_refs?   # Step 6 以后使用
idempotency_key?   # Step 7 以后使用
```

Step 0–5 只使用 CLI 文本输入。未来字段是版本化合同的一部分，但本轮不实现其业务行为。

`RuntimeEvent` 至少区分：

```text
request accepted
request queued
run started
assistant message
tool call
tool result
compaction completed
run completed
run failed
run cancelled
```

事件携带 `session_id`、`invocation_id`、服务端 `sequence_number`、`source` 和事件类型，供 CLI 展示及未来 Gateway 广播。

### 5.2 CLI 与 Gateway 共享 Session

CLI 与 Gateway 可以显式选择相同 `session_id`。不根据时间窗口自动合并 Session，也不使用客户端时间决定顺序。

“当前选择的 Session”属于每个入口或客户端自己的临时状态，不写入共享 Session/Memory 文件，也不由一个客户端替其他客户端修改。CLI 重启时默认选择 `updated_at` 最新的 Session；需要其他 Session 时再次显式执行 `/session switch <sessionId>`。

RuntimeHost 在接收请求时，于 Session 锁内分配单调递增的服务端 `sequence_number`。每个 Session 拥有一个逻辑邮箱：

1. 请求按服务端接收顺序进入邮箱；
2. 同一 Session 一次只执行一个完整 Agent Turn；
3. 新消息不能插入 assistant ToolCall 与 ToolResult 之间；
4. 当前 Turn 完成后才处理下一条消息；
5. 不同 Session 可以独立执行。

Step 0–5 的 CLI 使用内嵌 RuntimeHost。Step 6 若启用 Gateway，Gateway 进程成为该数据目录的唯一 RuntimeHost 和 Session 写入者；需要共享 Session 的 CLI 通过 Gateway 连接模式提交请求。禁止 CLI 与 Gateway 两个独立进程同时直接写同一 JSON 数据目录。

### 5.3 Scheduler Session

Scheduler 复用 Runtime 能力，但不复用 CLI/Gateway 的交互 Session。默认策略固定为 `per_run`：每次调度执行创建全新 Session，并记录 `job_id` 与 `run_id` 的所有权元数据。

这样每次运行没有旧对话 Token 开销。产生更多小型 Session 文件是已接受的权衡；保留和清理策略属于 Step 7，不进入本轮实现。

## 6. 核心模型与组件

### 6.1 持久领域模型

`Session` 至少包含：

```text
session_id
title
messages
summary
created_at
updated_at
next_sequence_number
owner?             # Step 7 以后使用
run_records?       # 仅保存需要审计的结构化记录
```

`Message` 至少包含稳定 ID、role、文本、结构化 ToolCall/ToolResult 关联、创建时间和确定顺序。Assistant Tool Turn 还要保存 `reasoning_content`，因为 DeepSeek thinking 模式要求后续 Tool 请求完整回传它；该字段只用于 provider 上下文，不向 CLI/Gateway 展示。模型必须能表达 `system`、`user`、`assistant` 和 `tool` 语义，但不能保存 DeepSeek SDK 或 HTTP 响应对象。

`Memory` 是跨 Session 的长期信息，使用独立 `memory.json`。Memory 只允许通过明确 CLI 命令修改，不从普通对话自动提取。

`ToolCall` 是 provider 输出的标准化请求；`ToolIntent` 是经过语法与 schema 验证、补充 effect 和目标资源后的执行意图；`AuthorizedToolCall` 只能由 PermissionPolicy 产生，并且是 ToolExecutor 唯一接受的调用类型。

`ToolResult`、`ApprovalRecord` 和 `ErrorRecord` 使用结构化状态。需要反馈给模型的错误会转换为经过清理的 observation；内部异常、Approval 审计记录和传输事件不直接塞入模型上下文。

### 6.2 核心组件

- `AgentRuntime`：统一 submit 接口和 RuntimeEvent 输出；
- `SessionCoordinator`：Session 加载、邮箱排队、序号、锁与保存；
- `ContextBuilder`：构造完整模型输入；
- `CompactionService`：预算判断、摘要生成和消息裁剪；
- `AgentLoop`：LLM、ToolIntent、Permission、ToolResult 闭环；
- `PermissionPolicy`：统一决策 `allow | ask | deny`；
- `ToolRegistry`：注册定义和执行器，但不替 AgentLoop 做权限判断。

核心 Ports：

```text
LLMClient
SessionStore
MemoryStore
TokenCounter
ToolExecutor
ApprovalPort       # Step 8 启用 ask 时使用
SkillProvider      # Step 9 使用
Clock
IdGenerator
```

Clock 和 IdGenerator 可注入，保证测试中的 ID、时间和顺序确定。

## 7. JSON 持久化

Step 0–5 使用 JSON：每个 Session 一个文件，另有一个 `memory.json`。只有 Store 实现可以进行直接 JSON 读写。

规则：

1. 所有持久文件顶层包含 `schemaVersion`；
2. Domain 模型与 JSON 表示通过 Store 显式转换；
3. 时间、ID 与列表顺序稳定；
4. 写入使用同目录临时文件和原子替换；
5. 写入失败时保留旧文件；
6. 损坏或不支持的 schema 必须报错，不能静默覆盖；
7. Store Port 必须有可复用于未来 SQLiteStore 的契约测试。

Goal 5 引入结构化 Tool 历史后，Session writer 使用 `schemaVersion: 2`，Memory 继续使用 v1。Session loader 严格支持 v1 和 v2：v1 读取为无结构化 ToolCall、无 reasoning、无 run records 的领域对象，首次正常保存时原子升级为 v2；不做启动时批量迁移，也不覆盖损坏文件。v2 Message 显式保存 `toolCalls`、`reasoningContent`、`toolName`、`toolResultStatus` 和 `toolErrorCode`，Session 显式保存 `runRecords`。不能在 v1 下偷偷增加可选字段。

保存边界：

1. 用户消息开始处理时保存；
2. assistant tool_calls 执行前保存；
3. 每个 ToolResult 完成后保存；
4. 最终 assistant message 保存；
5. 需要审计的 Permission、Error 与运行状态保存为 run record，不把所有传输事件永久化。

进程中断后，未完成 ToolCall 不自动重放。下次加载把对应运行标记为 interrupted，避免未来写 Tool 被重复执行。

CLI 需要支持：

```text
/exit
/session new
/session list
/session switch <sessionId>
/session rename <sessionId> <title>
/session delete <sessionId>
/memory add <content>
/memory list
/memory delete <memoryId>
/compact
```

这些命令由 CLI 处理，不作为普通用户消息发送给 LLM。

## 8. Context 与 Compaction

每次 LLM 请求按固定顺序构造：

```text
system prompt
soul
memory
session summary
未被压缩的完整 session turns
tool definitions / tool protocol
```

System Prompt 与 Soul 从 `sjtuclaw.prompts` 中随包安装的独立 UTF-8 配置文件加载，不依赖进程当前工作目录。System Prompt、Soul、Memory 和 Tool Definitions 是 stable context，Compaction 永不改写它们。

### 8.1 Token 预算

预算计算包括：

```text
stable context
+ session summary
+ candidate session messages
+ tool definitions / protocol
+ LLM_OUTPUT_RESERVE_TOKENS
```

默认在预计预算达到 `CONTEXT_BUDGET_TOKENS * 0.80` 时触发，压缩目标为不高于 `CONTEXT_BUDGET_TOKENS * 0.50`，并至少保留最近 4 个完整 Agent Turn。

ToolCall 与对应 ToolResult 属于同一 Agent Turn，不能拆分。自动和手动 `/compact` 必须使用相同 CompactionService，并输出压缩结果和摘要预览。

若不可压缩的 stable context 加输出预留本身已超过目标，Runtime 返回明确 ContextBudgetError，不能删除或改写 stable context。

### 8.2 TokenCounter

`TokenCounter` 是可替换 Port。默认模式使用经实施阶段验证的 DeepSeek 官方离线 Tokenizer；实施计划必须先验证官方集成方式，不能猜测包名。

若用户显式选择 approximate 模式，使用确定性的近似实现并在日志/状态中标记结果为 approximate。API 返回的 `usage.prompt_tokens` 需要保留用于观测和真实验收，但它发生在请求之后，不能替代请求前的本地预算判断。

实施阶段已确认 `deepseek-v4-flash` 的官方 Tokenizer 来源为 Hugging Face `deepseek-ai/DeepSeek-V4-Flash`，固定 revision 为 `60d8d70770c6776ff598c94bb586a859a38244f1`。V4 没有可直接使用的 Jinja Chat Template，也没有已确认的 DeepSeek 官方 V4 Tokenizer pip 包；`deepseek` 模式必须使用该 revision 中的 `encoding/encoding_dsv4.py::encode_messages()` 生成 prompt，再由本地 `tokenizer.json` 计数。运行时固定使用 `transformers==4.46.3` 与 `tokenizers==0.20.4`，只从 `DEEPSEEK_TOKENIZER_PATH` 加载本地 artifact，不能下载模型权重，也不能静默联网或降级。

`approximate` 模式采用确定性的 UTF-8 请求 JSON 字节数除以 4 并向上取整，只用于预算近似，不代表计费真值；Compaction 结果和 Runtime 事件必须携带 `token_counter_mode`。真实计费与回归观测仍以 API 的 `usage.prompt_tokens` 为准。

单元测试使用较小注入预算、Fake LLM 和确定性 TokenCounter，不调用真实 API，也不修改 `.env`。

## 9. LLM 与 Tool 协议

### 9.1 LLM Adapter

DeepSeek Adapter 负责：

- 把内部 LLMRequest 转为 OpenAI-compatible Chat Completions 请求；
- 把文本、usage 和原生 tool_calls 转为内部模型；
- 隐藏 HTTP/provider 细节；
- 对网络超时、HTTP 429 和 5xx 最多自动重试两次；
- 对 400、401、403、结构非法和 schema 错误不重试。

重试只发生在收到可执行 ToolIntent 之前，因此不会自动重放已经执行的 Tool。

### 9.2 Tool 模式

`LLM_TOOL_MODE=native` 使用 OpenAI-compatible 原生 tool_calls。

`LLM_TOOL_MODE=json` 使用严格文本 JSON 协议。响应除首尾空白外必须恰好是一个 JSON 对象，不允许 Markdown fence、前后解释文字、多个对象或随意字符串匹配。支持三种 envelope：

```json
{"type":"tool_call","tool":"read_file","args":{"path":"README.md"}}
```

```json
{"type":"tool_calls","calls":[{"tool":"current_time","args":{}},{"tool":"read_file","args":{"path":"README.md"}}]}
```

```json
{"type":"final","content":"最终回答"}
```

Tool 执行结果使用稳定输入 envelope 回传：

```json
{"type":"tool_results","results":[{"call_id":"call_001","tool":"read_file","status":"success","content":"安全 observation","error_code":null}]}
```

JSON 模式中的内部 call ID 由 IdGenerator 生成。单个 `tool_call` 归一化为单元素批次。`calls` 必须为 1–5 项，`args` 必须为对象。

JSON 模式不发送原生 `tool` role；Assistant ToolCall 历史规范化为文本 `tool_calls_history` envelope，连续 ToolResult 规范化为一个 user `tool_results` envelope。Native 模式发送 OpenAI-compatible `tools`、assistant `tool_calls`、`reasoning_content` 和 `tool_call_id`。两种模式都从同一结构化 Session 历史生成 provider 请求，不能把 provider 协议 JSON 当作领域持久格式。

两个 Adapter 最终都产生相同内部 `ToolCall`/final 模型。不能自动从 native 降级到 json，也不能把 malformed 内容当作可执行 Tool。

### 9.3 Tool 批次验证

执行前验证整个批次：

1. envelope/原生结构合法；
2. 数量不超过 5；
3. Tool 名已注册；
4. arguments 是合法 JSON 对象；
5. arguments 符合 Tool schema。

以上任意一项失败时，整批不执行。有合法 call ID 时，为每项生成结构化错误结果并反馈模型；JSON envelope 连基础解析都失败时，本次运行以 ToolProtocolError 结束。

整个批次通过结构与 schema 验证后，再逐项执行 PermissionPolicy。单个 ToolIntent 的 deny 不把其他合法且 allow 的 ToolIntent 变成非法；被拒绝项生成拒绝 ToolResult，允许项继续按顺序执行。

## 10. Agent Loop 与 Permission

一个 Step 定义为一次 LLM 响应及其请求的 Tool 批次。

```text
用户输入
→ ContextBuilder / 必要时 Compaction
→ LLM
→ final：保存并结束
→ tool calls：标准化并验证 ToolIntent
→ PermissionPolicy
→ ToolExecutor
→ 保存 ToolResult
→ 下一次 LLM
```

Tool 按模型请求顺序执行，不并行，保证 ToolResult 和持久化顺序确定。单个已验证 Tool 运行失败时，记录失败 ToolResult，并继续执行同批其他已验证只读 Tool。

### 10.1 PermissionPolicy

PermissionPolicy 固定返回：

```text
allow
ask
deny
```

Step 5：

- 已注册并满足路径策略的 `read_only` Tool 为 allow；
- 未知、越界、敏感文件、写入和执行能力为 deny；
- 不启用 ask 交互。

至少实现 `current_time`、`list_dir` 和 `read_file`。文件访问限制在配置允许的根目录内，并拒绝 `.env`、`.env.*`、`.git` 和越界路径；允许读取 `.env.example`。这是比课程最低要求更严格的安全边界。

Step 8 启用 ask 后：

1. 保存 PendingApproval；
2. 发出 ApprovalRequested；
3. 暂停当前完整 Turn；
4. 经授权 actor 决策后从原位置恢复；
5. 其他 Session 继续执行。

Scheduler 没有交互用户，`ask` 默认按 deny 处理；未来可由 Step 7/8 设计显式预授权规则。

### 10.2 循环限制

- `AGENT_MAX_STEPS=20`；0 表示兼容课程要求的无限模式；
- Tool signature 是规范化 Tool 名加按 key 排序的规范化 JSON arguments；
- 相同 signature 连续五次可以执行；
- 第六次及之后的连续相同调用被阻断，生成运行时错误 ToolResult；
- signature 改变时连续计数重置；
- 达到最大 Step 后不再请求 LLM；
- 若最后允许的 Step 执行 Tool 后仍无 final，运行以 `MAX_STEPS_REACHED` 结束；
- Ctrl+C 取消当前运行，保留最后一次成功原子保存。

## 11. 错误模型

错误类型至少包括：

```text
ConfigurationError
LLMError
StoreError
ContextBudgetError
ToolProtocolError
ToolValidationError
ToolExecutionError
AgentLimitError
InvocationCancelled
```

Runtime 将错误转换为统一 RuntimeEvent。CLI 负责展示，未来 Gateway 负责传输，但入口不能重新定义业务语义。

安全规则：

- 不输出 API key、Authorization header 或 `.env` 内容；
- malformed Tool 请求永不执行；
- Store 损坏不自动覆盖；
- Permission 缺失时有副作用 Tool 默认 deny；
- Approval/Error 审计记录与发送给模型的安全 observation 分离；
- Permission 是用户感知与执行控制边界，不宣称能替代 OS sandbox、容器或虚拟机隔离。

## 12. 测试与验收

### 12.1 确定性测试

默认测试组件：

- FakeLLMClient；
- DeterministicTokenCounter；
- FakeClock；
- FakeIdGenerator；
- pytest 临时数据目录。

覆盖：

- Context 固定顺序；
- Compaction 阈值、目标、摘要和完整 Turn；
- native/json ToolCall 标准化；
- strict JSON 与 schema 验证；
- allow/deny 和 ToolExecutor 不可绕过；
- 第六次重复调用阻断；
- Agent 最大 Step；
- JSON 原子替换、损坏文件和 schemaVersion；
- Store 契约；
- 同 Session 两个并发调用按服务端序号串行；
- 不同 Session 独立运行；
- Step 6–9 只做合同和架构审查；对应功能测试在各 Step 实现时新增。

### 12.2 真实 DeepSeek 强制验收

真实 DeepSeek 测试不是可选项。它只读取现有 `.env`，不修改 `.env`，不输出 secret，并使用无敏感信息提示词与独立临时数据目录。

强制覆盖：

1. 基础 Chat Completions；
2. 两轮对话与上下文记忆；
3. native tool_calls；
4. JSON Tool 兼容模式；
5. ToolResult 反馈后的最终回答；
6. `/compact` 的真实摘要；
7. `usage.prompt_tokens`；
8. 可安全触发的 API 错误映射。

真实测试顺序运行并控制调用数量。Fake 测试继续负责不适合真实 API 稳定复现的边界情况。

完成前运行：

```powershell
pytest -m "not live"
pytest -m live
ruff check .
mypy src tests
```

### 12.3 CLI 与用户验收分工

实施代理负责：

- 全部自动测试；
- 真实 DeepSeek 测试；
- CLI Session 创建、列出、切换、重命名、删除和重启恢复；
- CLI 多轮对话、Memory、`/compact`、两种 Tool 模式；
- CLI Permission、Error 与循环限制。

用户负责未来 UI 与 Gateway 的手工验收，包括真实浏览器体验、多设备连接和 Approval UI。实施代理必须提供逐项操作、预期结果和失败排查清单，不能把未执行的 UI/Gateway 检查声明为已验证。

## 13. Step 6–9 兼容边界

### Step 6 Gateway

- Gateway 把 HTTP/WebSocket/GUI 输入转换为 RuntimeRequest；
- Gateway 消费或广播 RuntimeEvent；
- CLI 与 Gateway 通过显式相同 session_id 共享 Session；
- Gateway 作为唯一 RuntimeHost/JSON 写入者；
- 附件仅以 AttachmentRef 进入请求，上传与存储策略由 Step 6 设计；
- Gateway 不复制 Agent Loop、ContextBuilder 或 Store 逻辑。

### Step 7 Scheduler

- 复用 RuntimeHost 与相同 RuntimeRequest；
- 默认每次运行创建全新 Session；
- 使用 job_id、run_id 和 idempotency_key 关联执行；
- 不写入交互 Session；
- ask 默认 deny；
- 保留与清理策略由 Step 7 决定。

### Step 8 Workspace 与 Approval

- ToolIntent 携带 effect 与目标资源；
- WRITE/EXECUTE 必须经过 PermissionPolicy；
- ask 通过 ApprovalPort 暂停和恢复；
- Workspace Tool 通过 infrastructure 实现，不绕过 ToolExecutor；
- Permission 不替代真正的进程/文件系统隔离。

### Step 9 Skill

- SkillProvider 在 ContextBuilder 阶段提供附加指令和 Tool 定义；
- Skill 不成为另一套 Agent，也不拥有独立 Agent Loop；
- Skill 需要文件或命令能力时仍通过 Tool 与 Permission 边界；
- Skill 不直接修改 Session JSON。

## 14. Step 10 SQLite 演进

未来新增 SQLiteStore，并通过以下配置选择后端：

```text
STORAGE_BACKEND=json|sqlite
```

不双写，不在正常启动时自动迁移。迁移必须：

1. 停止 Session 写入；
2. 验证所有 JSON 与 schemaVersion；
3. 创建带来源哈希清单的完整备份；
4. 在单个 SQLite 事务中导入；
5. 记录来源哈希与迁移标识，保证幂等；
6. 对比 Session、Message、Memory、ToolResult、Approval 和 Error 的数量与规范化内容；
7. 验证通过后提交事务；
8. 由用户显式切换 STORAGE_BACKEND；
9. 任何失败均回滚 SQLite，保留原 JSON。

JsonStore 与 SQLiteStore 必须通过同一套 Store 契约测试。当前 Runtime 和 Domain 模型不能依赖任一后端细节。

## 15. Living Spec 与 Git 提交

本文件是后续实施依据。实现期间若行为改变，必须在同一相关提交更新本 spec，不允许代码和规格长期分叉。

按可独立验证、可安全回滚的里程碑提交，预计边界：

1. `docs: add approved SJTUClaw core design`
2. `chore: scaffold Python project and quality checks`
3. `feat: add LLM client and multi-turn runtime`
4. `feat: add session persistence and stable context`
5. `feat: add token-based compaction`
6. `feat: add tool loop and permission policy`
7. `test: add live DeepSeek and CLI acceptance coverage`

实际实施计划可以调整边界，但不为提交数量拆分无意义 commit。每次提交前运行相关测试，检查 `git status --short` 与 staged diff，只暂存当前里程碑文件，并报告 commit hash、验证结果和回滚边界。

始终排除：

- `.env`；
- 用户提供且未跟踪的 `SJTUClaw.md`；
- 用户已有的 `instruction_doc.md` 删除；
- `.superpowers/` 可视化伴侣文件；
- 失败或未完成状态，除非用户明确要求保存检查点。

## 16. 明确偏离课程最低要求的安全决策

以下差异是有意设计，不是遗漏：

1. 课程 Step 5 不要求 Agent Turn 总上限；本设计默认 20，设置 0 可恢复无限模式。
2. 课程 JSON 示例允许从混合文本中尽量抽取协议对象；本设计要求严格 envelope，malformed 内容绝不执行。
3. 课程 Step 5 不强制目录沙箱或敏感文件过滤；本设计限制允许根目录并拒绝 `.env`、`.git` 和越界路径。
4. 课程只要求只读 Tool；本设计从 Step 5 引入 PermissionPolicy 的 allow/deny 边界，为 Step 8 的 ask 保持兼容。
5. UI 与 Gateway 不属于本轮实现，未来由用户执行最终手工验收。

## 17. 已知风险

- DeepSeek 官方离线 Tokenizer 的具体集成方式必须在实施计划首段通过官方资料和最小实验验证；不能假设第三方包名。
- 真实 LLM 测试可能受限流、网络和模型非确定性影响，因此数量受控，并与确定性 Fake 测试并存。
- JSON 后端只支持单 RuntimeHost 写入；跨进程并发写必须等待更强协调方案或 SQLiteStore。
- `per_run` Scheduler 会生成较多 Session 文件；Step 7 必须独立设计保留和清理策略。
- PermissionPolicy 是执行控制和用户感知边界，不是安全沙箱。

上述风险均不阻塞 Step 0–5 实施计划。
