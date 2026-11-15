# SJTUClaw Step 6–9 设计

- 状态：已批准，待生成实施计划
- 日期：2026-07-19
- 批准日期：2026-07-19
- 依据：课程 `SJTUClaw.md` Step 6–9
- 前置：`docs/superpowers/specs/2026-07-15-sjtuclaw-core-design.md`

本文记录已经确认并获用户批准的产品与架构决策。下一步先生成实施计划，再按计划分阶段实现。

## 1. 总体架构

### 1.1 范围

- 面向本机单用户，Gateway 默认仅监听 `127.0.0.1`。
- FastAPI、Scheduler、CLI 共用一个 `RuntimeHost`、`SessionStore`、`ContextBuilder`、`CompactionService`、`ToolRegistry` 和 `AgentLoop`。
- 保持模块化单体和单进程 JSON writer；入口只提交 RuntimeRequest 或控制命令。
- LLM API key 只存在服务端本地配置。
- Step 10 仅保留 SQLite 迁移模型，不在 Step 6–9 实现数据库或双写。

### 1.2 主要模块

```text
React UI / CLI
    -> Gateway / CLI adapter
Scheduler -> RuntimeHost -> AgentLoop -> LLM / ToolRegistry
    -> Session / Attachment / Task / Workspace / Skill services
    -> Store ports -> JSON stores
```

- RuntimeHost：持久化队列、每 Session 串行、跨 Session 公平并发、Run 控制与恢复。
- AgentLoop：上下文、真实流式 LLM、Skill、Tool、Permission、步数/重复限制及消息保存。
- 各 Service：领域校验；各 Store：唯一 JSON I/O 入口。
- Bootstrap：创建共享 Clock、ID generator、Stores、Registry、RuntimeHost、Scheduler 与 Gateway。

### 1.3 数据版本

- Session v3（Step 6）：PendingRequest、附件引用、Run 生命周期、`durationMs`、`assistantMessageId`。
- Session v4（Step 8）：WorkspaceBinding、PendingApproval、ApprovalDecision、可恢复 Run checkpoint。
- Session v5（Step 9）：显式 Skill refs、SkillUsage、Skill snapshot/hash 与选择顺序。
- Task、Occurrence、Attachment metadata、Skill manifest 使用各自独立 schema version。
- Writer 只写当前版本；reader 严格支持已声明旧版本。对象首次正常保存时原子升级。
- ID、时间戳、序号、排序、错误码和 JSON schema 保持稳定；拒绝未知未来版本和损坏数据。

## 2. Step 6：Gateway 与同源本地网页

### 2.1 技术栈与本地认证

- 后端：FastAPI；前端：React + TypeScript + Vite。
- 正式运行由 FastAPI 同源提供网页、REST、SSE 和构建后的静态文件，只需启动一个 Gateway。
- 前端源码与锁文件进入版本控制，不使用公共 CDN。
- 每次启动生成至少 256-bit 的内存 bootstrap token，并用 URL fragment 传给网页。
- 前端兑换 token 后立即清除 fragment；Gateway 设置 `HttpOnly`、`SameSite=Strict`、`Path=/` 的不透明 Cookie。
- Gateway 重启后旧 Cookie 失效。业务 API/SSE 校验 Cookie、Host；状态修改和 SSE 同时校验严格同源 Origin。
- Markdown 支持标题、列表、表格、链接和代码块；原始 HTML关闭，输出经 allowlist 清理并配合严格 CSP。

### 2.2 页面与交互

- 三栏工作台：左侧 Session；中间聊天；右侧活动与附件。聊天区为视觉中心。
- `>=1200px` 显示三栏；`800–1199px` 右栏为抽屉；`<800px` 左右栏均为抽屉。
- 抽屉支持关闭按钮、Escape、焦点圈定与恢复。折叠不影响 Run、SSE、附件和 Session 状态。
- 临时配色：`#162033 / #f5f7fb / #fff / #4f7cff / #dfe5ef / #e7efff / #edf1f7 / #263247 / #60708a`，统一为 design tokens。结构完成后再美化颜色和图标。
- 用户只能显式选择已有 Session 或创建新 Session。首次无 Session 时调用创建 API；非法或缺失 `session_id` 返回错误，不自动改投新 Session。

### 2.3 真实流式、进度和时间

- DeepSeek 使用真实 streaming；Adapter 输出 provider-independent `ASSISTANT_DELTA`。
- 前端短窗口合并增量并持续渲染。完整 Assistant Message 只在流成功结束后保存。
- `reasoning_content` 仅供 DeepSeek 历史回放，不进入网页和 Gateway 事件。
- 中断的 Assistant 文本从 UI 清除，不进入 Session 或下一轮上下文。
- Runtime 发结构化进度码，前端映射文案：

```text
request.queued
context.preparing
model.started
tool.running
model.synthesizing
run.completed / run.failed / run.cancelled
```

- UI 可显示“正在理解问题、正在调用搜索工具、正在读取文件、正在整理上下文、已完成”。进度文案不持久化。
- 单轮计时从用户点击发送开始，到最终文本渲染并收到终态结束；包含上传完成后的排队、LLM、Tool、网络与审批等待。
- 处理中显示 `处理中：xx s`；完成后显示 `总耗时：xx s`。
- 服务端以单调时钟为准，`RUN_COMPLETED` 保存整数 `duration_ms` 和 `assistant_message_id`；历史刷新后仍显示总耗时。
- 附件在发送前完成的上传/提取、单个 Tool/阶段耗时不计为独立展示项。

### 2.4 REST、SSE 与错误

- REST 管理 auth、Session、Request/Run 控制、附件、Task、Workspace、Approval、Skill 和 Download；外部路径统一使用 `/api/v1`。
- SSE 传输队列、进度、增量、Tool、Compaction、Approval 和终态事件。
- 每个 Run 的 SSE event ID 严格递增；浏览器断开不取消 Run。
- Gateway 保存有界内存 replay buffer。缓存缺口时客户端重新读取 Session/Run 快照，不拼接残缺文本。
- REST 错误统一为稳定 `code + safe message + requestId + details`，不返回 traceback、秘密、敏感绝对路径或原始 provider body。

### 2.5 附件

- 用户选择文件后立即上传、校验和提取；状态为 `uploading / processing / ready / failed`。
- 只有 `ready` 附件能随消息提交。新附件默认选中；消息成功入队后 Composer 选择清空，附件仍归属 Session。
- 支持严格 UTF-8 文本和带文本层 PDF；不含 OCR、图片、Word 或多媒体解析。
- 上传流式写临时文件并计算 SHA-256；PDF 在可终止辅助进程中预提取。成功后原子发布 original、normalized text、hash 与 metadata。
- ContextBuilder 只提供所选附件 metadata。模型用 Session-scoped `read_attachment` 分段读取派生文本。
- 单独删除只允许“从未进入已持久化 User Message，且当前未被 PendingRequest 引用”的附件；冲突返回 `ATTACHMENT_IN_USE`。
- 默认限制：单文件 20 MiB、每消息 10 个、每 Session 200 MiB、PDF 300 页、提取 2,000,000 字符、单次读取 32,000 字符。

### 2.6 队列、并发与控制

- 同一 Session 同时一个活动 Run；不同 Session 全局最多并发 2 个，采用公平 ready-session 轮转。
- PendingRequest 保存在 Session v3。接收成功后显示“队列中”；Gateway 重启按原顺序自动续行。
- `PendingRequest -> User Message + RUN_STARTED` 使用一次 Session 原子保存。
- 队列项可单独取消，不可编辑或重排；取消成功为 `CANCELLED_BEFORE_START`。修改内容使用“取消后重新发送”。
- “停止”只取消当前 Run，下一条队列请求自动开始；“清空队列”是独立操作。
- 活动 Run 提供“引导”按钮；普通发送默认排队，只有点击“引导”才走同一 Run 的 RunControl 通道。
- 同一时刻最多一个待应用引导；每 Run 最多 5 次。引导在安全点应用，丢弃未完成流式文本，并作为 `input_kind=steer` User Message 保存。
- 每个初始/引导段独立使用 `AGENT_MAX_STEPS=20`；引导后重复 Tool Call 计数清零。默认单 Run 最多 120 个 LLM 步骤。
- 引导、停止和自然完成竞态以服务端接受顺序为准。
- 活动 Run 崩溃后保留 User Message，记录 `RUN_INTERRUPTED_GATEWAY_RESTART`，不自动重放 LLM/Tool；用户可手动重试。

### 2.7 流式与 SSE 详细合同

- LLM Adapter 在内存中分别组装文本、`reasoning_content` 和 Tool Call fragments。Tool 名称和参数完整且通过结构校验后，才发布 TOOL_CALL 或进入执行。
- 原生 DeepSeek 历史继续回放 Assistant `reasoning_content`；JSON mode 使用既有严格 envelope。两种协议统一产出内部 ToolCall/ToolResult。
- SSE `id` 是单 Run 严格递增序号；`event` 是稳定类型；`data` 至少含 `schemaVersion`、`eventId`、`sessionId`、`requestId`、`runId`、`sequenceNumber`、`occurredAt` 和受限 payload。
- 首次订阅先发当前快照，再发增量；重连使用 `Last-Event-ID`。同一事件 ID 内容不可改变。
- replay buffer 超窗时发送 `REPLAY_WINDOW_EXPIRED`，前端重新读取 Session 与 Run 终态。禁止把缺失前缀和新 delta直接拼成答案。
- `ASSISTANT_DELTA` 只携带回答文本；reasoning、未完成 Tool 参数、原始异常和秘密字段不进入 SSE。
- 终态事件最后发送且恰好一次；前端收到终态后进行完整 Markdown 重渲染、停止计时并用服务端 duration 校正。
- Gateway 周期发送无业务含义 heartbeat；网络断开只影响观察通道。

### 2.8 进度与持久化边界

- 前端根据稳定状态码映射中文，不根据自由文本猜阶段。`tool.running` 只带清理后的 Tool 名称和短资源标识。
- 进度、展开活动、heartbeat 和中间计时只存在于前端状态及短期 SSE buffer，不进入 Message、Session JSON 或 LLM 上下文。
- User Message、完整 Assistant Message、ToolCall/ToolResult、RunRecord 和 SkillUsage 各自遵守明确保存边界。
- 页面刷新或 Gateway 重启后，从 `RUN_COMPLETED.durationMs + assistantMessageId` 恢复“总耗时”；非完成终态不伪造完成耗时。
- 跨重启续行的排队请求用持久化 UTC 提交/完成时间恢复总耗时，并在审计中标记 recovered timing；系统时间异常时显示计时不可用，不生成负值。

### 2.9 附件上传、删除与读取边界

- 上传请求采用同文件系统临时文件、流式字节计数和 SHA-256。文本严格 UTF-8 解码；PDF parser 使用页数、字符数、内存和总时间限制。
- 加密 PDF、无文本层 PDF、非 UTF-8、格式不支持、超限和解析失败均返回结构化错误，清理临时文件且不发布 Attachment。
- `read_attachment` 参数只有 opaque attachment ID、读取上限和不透明 cursor；Session ID 与存储根来自 ToolExecutionContext。
- 附件 ID 不属于当前 Session、文件损坏或派生文本缺失时返回安全错误，不泄露物理路径。
- 删除与消息接收/Run 启动共用引用一致性边界：删除先生效时发送失败；发送先生效时删除返回 `ATTACHMENT_IN_USE`。
- 正在上传/提取的附件可以取消并清理；取消未开始的 PendingRequest 后，若附件仍未进入历史且没有其他引用，则恢复可删除。
- 原始 PDF保留用于用户下载或以后复制到 Workspace；AgentLoop 只读取预生成 normalized text。

### 2.10 Session 路由与附件 Composer

- 首次进入：Session 列表为空时显式创建；否则恢复该浏览器上次选择，找不到时选择最近更新 Session。
- 当前 Session 是浏览器状态，不写入共享 Session JSON。旧标签页、并行标签页和直接 API 请求仍由服务端最终校验。
- 新上传附件在 Composer 默认选中，用户可取消本轮选中。消息成功持久化为 PendingRequest 后才清空本轮选择。
- 发送时所有 attachment refs 必须同属请求 Session；任一非法则整批拒绝，不能截断或静默忽略。
- Composer 中仍被选中的 `uploading/processing` 附件会阻止发送；用户可先移除它再发送纯文本。

### 2.11 引导安全点

- 引导使用独立 RunControl channel，以 `session_id + run_id + steer_id + idempotency key` 定位；不通过普通 `runtime.submit` 排队。
- 构建上下文时：停止当前构建，保存引导 User Message，再重新构建。
- LLM streaming 时：取消并等待 provider stream 清理，丢弃未完成 Assistant 文本，再应用引导。
- Assistant ToolCall 已保存但结果未齐时：为尚未执行的调用保存明确 interruption ToolResult，保持协议闭合。
- 只读 Tool执行中：尝试取消并保存真实终态。Update/Shell/Download 已开始后不宣称回滚，报告实际或未知副作用。
- Session 原子保存中：等待保存完成后再应用引导。最终 Assistant 已持久化后拒绝引导为 `RUN_NOT_ACTIVE`。
- 事件至少包含 `steer.accepted / steer.waiting_safe_point / steer.applied / steer.rejected`；UI 清除旧 partial并显示“正在应用引导”。

### 2.12 PendingRequest 与崩溃恢复

- Gateway 只有在 PendingRequest 原子保存后才返回“队列中”。保存内容至少包括 request ID、接收序号、提交时间、文本、附件 refs、来源、actor 与幂等键。
- 请求真正开始时，一次保存完成 PendingRequest 移除、User Message 追加和 `RUN_STARTED`。该保存成功后才调用 LLM/Tool。
- 启动时先对账无终态的活动 Run，再恢复各 Session 队列；已取消项不会重新入队。
- `RUN_STARTED` 且无终态的 invocation 默认写入一次 `RUN_INTERRUPTED_GATEWAY_RESTART`。尚未决策的持久化 PendingApproval 是可恢复 checkpoint。
- 已持久化幂等键保证客户端响应丢失后的重发返回原 request ID 和状态。
- 跨 Session 调度采用全局许可与 ready-session 队列：一个 Session 完成后如仍有任务，回到队尾，让其他 Session 获得机会。

### 2.13 Bootstrap、Cookie 与浏览器边界

- Gateway 启动时生成一次性 bootstrap token，并可自动打开 `http://127.0.0.1:<port>/#bootstrap=<token>`。fragment不会随 HTTP请求、Referer或常规访问日志发送。
- 前端只兑换一次，成功后用 `history.replaceState`清除 fragment；token与会话 Cookie均不进入 localStorage/sessionStorage。
- Cookie是当前 Gateway进程的本地会话凭证。静态入口/bootstrap之外的 Session、Run、SSE、附件、Task、Approval、Download、Skill API全部验证 Cookie。
- Host校验覆盖全部请求；Origin校验覆盖状态修改和 SSE。该设计防止同机恶意网页借浏览器调用本地 Gateway，但不声称抵御已经控制本机进程的恶意软件。
- 非 loopback监听属于新的部署/威胁模型，不能由普通配置静默开启。

### 2.14 Markdown/XSS 处理

- 模型输出、ToolResult、附件文本和历史消息都按不可信内容处理。XSS指恶意文本诱导浏览器执行 HTML/JavaScript并借当前页面权限调用 Gateway。
- Markdown renderer关闭 raw HTML，以 allowlist清理生成 DOM；代码块始终作为文本显示。
- 链接过滤危险 scheme；script、事件属性、iframe和可执行 SVG/HTML被移除。CSP是第二层保护，不替代 sanitizer。
- 流式阶段走同一安全渲染管线；终态用完整累计 Markdown做确定性重渲染。Session只保存原始 Markdown文本，不保存 HTML。

### 2.15 三栏与响应式细节

- 左栏包含产品入口、新建 Session、Session列表、活动/排队标记和“定时任务”导航。
- 中栏包含 Session标题、历史、流式回答、进度、计时、Composer、附件和内联 Skill mention。
- 右栏上部显示瞬时运行活动/Approval，下部显示当前 Session附件；不复制完整聊天历史和 reasoning。
- 折叠右栏按钮显示活动指示和附件数量。切换视图/抽屉不会重建 SSE连接或丢失 Composer。
- 临时图标使用内置 SVG/文本；后续配色和图标美化只能替换 design tokens/资源，不改变布局、API和状态合同。

### 2.16 队列取消与停止竞态

- 队列取消以 `session_id + request_id`定位，并在队列锁内与 Run启动竞争。取消先成功则移除 PendingRequest、写终态且永不进入 AgentLoop。
- 启动先成功则取消队列返回 `RUN_ALREADY_STARTED`，不会静默转换为活动 Run停止。重复取消已取消项返回当前终态。
- 取消队列请求不追加 User Message，只移除该请求的附件 refs；原附件继续归属 Session。
- 停止活动 Run以 `session_id + run_id`定位，先保存真实取消终态再释放 Session执行权。下一项按接收顺序自动启动。
- 停止耗时计入被停止 Run；下一队列项从它最初发送时计时，因此包含等待时间。

### 2.17 Tool 与外部请求超时

- 服务端为 LLM、HTTP、附件处理和每类 Tool设置连接、读取、总执行与清理上限。模型只能请求更短时间。
- Tool timeout生成 `TOOL_TIMEOUT` observation，Agent可在原有步数、重复和 Permission限制内降级或解释；Runtime本身不做无界自动重试。
- 进程型 Tool终止进程树并等待清理；HTTP Tool分别设置 connect/read/total timeout。
- Timeout不会使 Gateway退出，也不会回滚已成功保存的 Message/ToolResult；前端仍不显示单 Tool耗时。

## 3. Step 7：Scheduler 与定时任务

### 3.1 计划模型

- 支持 `once`、`interval`、`daily`、`weekly`；不支持 cron 和自然语言计划。
- 绝对时间保存为 UTC；日历规则同时保存 IANA timezone，UI 默认 `Asia/Shanghai`。
- 固定间隔最短 5 分钟：`SCHEDULER_MIN_INTERVAL_SECONDS=300`。
- Task 保存文本指令、所属 Session、结构化 schedule、状态和 `next_run_at`；Step 7 Task 不携带附件。

### 3.2 触发与恢复

- 停机期间错过触发：重启后补执行一次；周期任务错过多次合并为一个 `coalesced` Occurrence。
- 下一次触发始终从原 schedule anchor 计算，避免漂移。
- 周期单次失败后记录失败并继续下一个正常计划点；一次性失败由用户手动重试。
- 同一 Task 已有 Occurrence 排队或执行时，新触发记录 `SKIPPED_OVERLAP`，不形成积压。
- 取消 Task：停止未来触发，取消尚未开始的 Occurrence，已活动 Run继续。状态机为 `CANCEL_REQUESTED -> CANCELLED`，可重启恢复。
- Scheduler 用稳定 `occurrence_id` 派生 RuntimeRequest 幂等键，并通过同一 RuntimeHost、并发限制和 AgentLoop 执行。

### 3.3 TaskStore 与 UI

```text
scheduler/tasks/<task_id>.json
scheduler/occurrences/<task_id>/<sequence>-<occurrence_id>.json
```

- 每 Task 一个状态文件，每 Occurrence 一个历史文件；序号递增，历史按 cursor 分页。
- Occurrence 保存 Run/Assistant Message 关联，不复制完整 Assistant 正文或 ToolResult。
- 左侧增加“定时任务”入口；中栏显示创建/列表，右栏显示规则、取消和 Occurrence 历史。
- 创建页区分“工作方法（Skill）”和“高级 Tool 权限”。

### 3.4 托管权限与 Skill

- Scheduler Run 无交互审批。创建 Task 时用户可勾选：Workspace 写入、Shell 命令、创建下载入口。
- 授权只绑定当前 `task_id + session_id + workspace_binding_revision`。勾选类别通过普通安全校验后自动执行；未勾选返回 `SCHEDULED_PERMISSION_DENIED`。
- 自动授权不增加逐次 Approval 记录；AgentLoop 仍保存唯一 ToolCall/ToolResult，Occurrence 只保存关联。
- Task 可显式固定 0–3 个 Skill 版本；Scheduler Run 不再自主选择 Skill。
- 修改指令、Skill、权限或 Workspace 使用“取消旧 Task 并创建新 Task”，保留旧历史。

### 3.5 规则校验与 next_run_at

- `once` 必须是明确未来时间；`interval` 使用正整数秒与稳定 anchor；`daily/weekly` 使用当地时间、IANA timezone 和非空星期集合。
- API 与持久化的绝对时间统一为 UTC，保留原 schedule 便于 UI 解释。前端预览仅供填写，服务端计算结果才是事实。
- 过去的一次性时间、非法时区、空星期、间隔不足 300 秒、溢出和无法计算未来触发点的规则整单拒绝。
- 周期 occurrence 结束后从原 anchor 计算严格晚于当前时刻的第一个计划点，不按执行结束时间顺延。
- Scheduler 使用可注入 Clock 和最近 deadline/wakeup 机制：每轮先清除 `_wakeup_event`，再对账并处理到期任务，随后按最早 `next_run_at` 计算动态等待时间，不做固定间隔全表轮询。
- Task 创建成功或进入取消流程后，在持久化状态更新完成后设置 `_wakeup_event`。新建更早 Task、取消当前最近 Task 时，后台等待立即中断并重新计算 deadline；进程关闭同时设置 stop 与 wakeup，不能被远期 deadline 阻塞。
- `_wakeup_event` 是进程内通知，不是持久事实；信号合并不丢任务，因为每次唤醒都重新扫描持久 TaskStore。测试用虚拟时钟和受控异步事件验证，不通过真实长时间 sleep 或降低生产下限验证。

### 3.6 Occurrence 状态与历史

- Task 的计划状态与 Occurrence 的执行状态分离。Occurrence 至少区分 planned、queued、running、completed、failed、cancelled-before-start、skipped-overlap。
- 补执行记录错过窗口、合并次数、实际入队时间和 `coalesced` 类型；它只形成一个 Agent Run。
- 单次失败保留错误关联，周期 Task 继续启用；一次性失败进入终态。手动重试新建 Occurrence，并用 `retry_of_occurrence_id` 关联旧记录。
- `SKIPPED_OVERLAP` 记录原计划时间和阻塞它的 Occurrence ID，不进入 Session Message 或 LLM 上下文。
- TaskStore 负责严格 schema、ID/路径保护、原子替换、独占创建、稳定序号和 cursor。
- Assistant 完整正文以 Session Message 为唯一事实；Occurrence 保存 invocation、assistant message ID、状态与安全错误摘要。
- 单个损坏 Task/Occurrence 停止相关调度并在 UI 显示数据错误；其他有效 Task 继续运行，损坏文件不被自动覆盖。

### 3.7 启动对账与取消竞态

- Scheduler 创建 Occurrence 后先持久化，再提交带稳定幂等键的 RuntimeRequest。提交响应丢失时恢复同一个 Occurrence。
- 启动恢复按 Occurrence、Session PendingRequest 和 RunRecord 对账：已入队的不重复提交，已活动的不重放，已终态的不重新触发。
- Task 取消先持久化 `CANCEL_REQUESTED`，立即阻止新 occurrence，再逐个取消未开始请求，最后保存 `CANCELLED`。
- 取消与 Run 启动在 RuntimeHost 队列锁内竞争。取消先到则 `CANCELLED_BEFORE_START`；启动先到则活动 Run 完成真实结果，但不再产生未来触发。
- 取消重复调用幂等，Task、Occurrence 和历史保留，`next_run_at` 置空。

### 3.8 Task 创建与管理界面

- 创建表单包含 Session、文本、计划类型、时区和规则字段，并展示规则摘要与 next run预览。
- 列表展示内容摘要、类型、Session、状态、规则、`next_run_at` 和最近结果；支持按状态/Session 筛选。
- 详情展示完整规则、取消操作和可分页历史，区分成功、失败、补执行、重叠跳过及入队取消。
- Occurrence 可跳转到对应 Session Message/Run。切换“对话/定时任务”只改变 UI，不停止 Runtime 或 Scheduler。
- 中等宽度用右侧抽屉显示详情；窄屏使用分层页面/抽屉并保持焦点和返回路径。
- 创建、查询、取消、重试全部通过 Gateway；浏览器不自行设置定时器触发 Agent。

## 4. Step 8：Workspace、Advanced Tool 与 Approval

### 4.1 Workspace

- 每 Session 最多一个显式 WorkspaceBinding，包含稳定 ID、规范根路径、显示路径与 revision。
- 首次绑定、更换或解除只在该 Session 无活动 Run、无 PendingRequest 时进行；冲突返回 `WORKSPACE_BUSY`。
- 已绑定时，`list_dir/read_file` 与高级文件 Tool 都以 Workspace 为根；未绑定时只读 Tool 兼容全局 `TOOL_ROOT`，高级 Tool 返回 `WORKSPACE_NOT_CONFIGURED`。
- 已绑定但路径失效时返回 `WORKSPACE_UNAVAILABLE`，不回退旧根。
- ToolExecutionContext 提供受信任的 Session、Workspace revision/root、Task/Skill 信息；模型只传相对路径和业务参数。
- PathGuard 继续保护越界、符号链接、`.git`、`.env/.env.*`；`.env.example` 可用。Workspace 是应用边界，不是 OS Shell 沙箱。

### 4.2 普通 Approval

- 普通聊天中的 WRITE/EXECUTE/Download ToolCall 先持久化 PendingApproval，再暂停同一 Run。
- UI 显示 Tool、效果等级、目标、命令或 diff 预览与风险；提供“仅批准本次”和“拒绝”。选择拒绝时可填写简短原因。
- Approval 以单个 ToolCall 为单位；决定后重新校验 Tool、参数、Workspace revision 和目标。
- 等待期间 Session 保持活动、后续消息排队，但释放全局并发许可。
- `APPROVAL_TIMEOUT_SECONDS=1800`；30 分钟后生成 `APPROVAL_TIMEOUT` ToolResult并继续同一 Run。
- 刷新或 Gateway 重启恢复未决审批。已开始执行但 ToolResult 未保存的 Tool 不自动重跑，Run 终结为 `APPROVED_TOOL_EXECUTION_INTERRUPTED`。
- 交互式 CLI 支持本次批准或拒绝，并可在拒绝时输入原因；非交互 CLI 直接拒绝 ask，使用稳定系统原因。

### 4.3 Attachment Copy 与 Update Tool

- 按课堂要求注册 `copy_attachment_to_workspace`：把当前 Session 的 `ready` 附件原文件复制到 Workspace 内指定相对路径。
- Attachment ID 只作为不透明业务参数；Session、AttachmentStore 与 Workspace root/revision 来自 ToolExecutionContext。跨 Session、未就绪或损坏附件直接拒绝。
- 该 Tool 属于 WRITE：普通聊天逐次 Approval；Scheduler 只有获得当前 Task 的“Workspace 写入”授权才可执行。
- 复制使用有界流、SHA-256、临时文件和原子 replace；创建要求目标不存在，覆盖要求 `expected_sha256`，不经由 normalized text 改写原文件。

- 一个稳定 Tool，以严格 `write / patch` 联合 schema 支持整文件创建/覆盖和单文件补丁。
- 只处理 Workspace 内 UTF-8 文本；Step 8 不含删除、重命名、chmod、二进制或跨文件事务。
- 创建声明目标不存在；覆盖/补丁携带 `expected_sha256`。
- Approval 展示有界 diff；执行前再次校验 hash、revision 和路径。冲突返回 `FILE_CHANGED_SINCE_APPROVAL`。
- 候选内容通过临时文件和原子 replace 提交；支持显式 `create_parents`，每级目录仍受 PathGuard。

### 4.4 Shell Tool

- 按课堂要求注册 `new_shell` 与 `run_command`。`new_shell` 为当前 Session 的活动 Run 启动一个持久、非交互 Shell；已有 Shell 时先有界终止旧进程树。
- 新 Shell 的初始 cwd 默认为 Workspace 根，也可指定 Workspace 内相对子目录；一个 Session 同时最多一个 Shell，绑定 invocation 与 Workspace revision。
- `run_command` 只向当前 Session 已启动的 Shell 发送命令；多次调用复用 cwd、环境变量和 `source` 等 Shell 状态。未启动时返回 `SHELL_NOT_STARTED`。
- 执行前后都读取并校验真实 cwd。Shell 离开 Workspace 时终止进程树并返回 `SHELL_LEFT_WORKSPACE`；该检查不等同于 OS 沙箱。
- 无交互 TTY/stdin，Windows 默认隐藏窗口。子进程只继承 allowlist 环境；API key、bootstrap token、Cookie 等秘密不进入环境。
- 默认命令超时 120 秒、最大 900 秒、stdout/stderr 合计 1 MiB。启动、停止、引导、超时或协议失步时有界终止整个 Shell 进程树；后续命令须重新 `new_shell`。
- `run_command` 返回命令、执行前后 cwd、退出码、stdout、stderr、是否超时、是否截断和稳定错误信息。
- Run 进入 completed、failed、cancelled 或 interrupted 终态时关闭 Shell；不支持跨 Run 或跨 Gateway 重启保留 Shell 状态。
- Shell 命令仍可能通过绝对路径或程序自身访问 Workspace 外部与网络；普通聊天逐次审批，Task 的长期 Shell 类别授权必须醒目提示风险。

### 4.5 Download Tool

- 为 Workspace 普通文件创建不可变快照；链接 10 分钟内可重复下载。
- 快照流式复制并计算大小/SHA-256，成功后原子发布随机 download ID。
- 下载同时校验本地 Cookie、Session 归属和 ID，响应使用 `Content-Disposition: attachment` 与 `nosniff`。
- 到期或 Gateway 重启清理快照；ToolResult 保存 ID、文件名、hash、大小、`expires_at`，不保存临时绝对路径。

### 4.6 Workspace 绑定与工具根竞态

- WorkspaceService 在 Session 协调锁内同时检查活动 Run、PendingRequest 和 binding revision。检查与保存之间不释放可插入请求的锁。
- 消息或 Scheduler 入队先成功时，Workspace 修改返回 `WORKSPACE_BUSY` 和安全的活动/排队数量；绑定先成功时，新请求使用新 revision。
- 附件上传/提取不使用 Workspace，因此不单独阻止绑定修改；已接受的消息请求会阻止修改。
- 每次 Tool 执行重新验证真实路径、revision、目录身份和访问状态。绑定目录被移动或替换时返回 `WORKSPACE_UNAVAILABLE`。
- `read_attachment` 始终使用 AttachmentStore 的 Session 边界，与 Workspace/TOOL_ROOT 相互独立。
- Workspace 变更审计保存 binding ID、revision 和清理后的显示信息；绝对根路径不进入 LLM 上下文。

### 4.7 Approval 状态机

- Assistant ToolCall 和 DeepSeek replay 字段先保存，再保存 PendingApproval checkpoint，最后发送 `APPROVAL_REQUIRED`。
- PendingApproval 至少关联 approval ID、invocation、ToolCall、受信任 ToolIntent、目标资源、原因、状态、创建/过期时间。
- 一个模型批次含多个 ask ToolCall 时按原顺序逐个处理；批准一个调用不会授权同批或后续调用。
- Approval decision payload 含决定、幂等 key，以及仅 `deny` 可携带的可选短原因；不能修改 Tool 参数。AgentLoop 按 approval ID 取回原 ToolIntent。
- 拒绝原因按普通用户文本处理：严格 UTF-8、去除首尾空白、拒绝控制字符并限制长度；原文保存到 ApprovalDecision，清理后放入 `APPROVAL_DENIED` ToolResult 供模型继续判断。
- 拒绝生成 `APPROVAL_DENIED`；自动过期生成 `APPROVAL_TIMEOUT`，decision actor 为 `system_timeout`，不伪造用户原因。
- 用户决定与超时在同一 RunControl 锁内竞争，先持久化者生效；相同规范化 payload 重试幂等，决定或原因不同则返回冲突。
- 等待到期后，同一 Run 重新进入公平调度取得全局许可。模型可根据拒绝/超时结果结束或提出新方案。
- 用户停止等待中的 Run 时，同时终结 PendingApproval；后到 timeout 不会唤醒已取消 Run。
- Gateway 重启后，未决 `new_shell` 经批准可重新校验并启动；未决 `run_command` 因进程状态不可恢复而生成 `SHELL_NOT_STARTED`，不能暗中创建新 Shell。

### 4.8 Scheduler 托管授权合同

- Task 创建页始终显示三类权限及风险，不做权限试运行，也不配置具体命令/路径 allowlist。
- 当前 Task、Session、Workspace revision 三者全部匹配时，已勾选类别可自动授权；Workspace revision 改变后旧 grant 失效。
- 未勾选类别直接返回 ToolResult，不生成审批卡、不等待 30 分钟。
- READ_ONLY Tool仍按基础策略允许。Skill 不是 Permission source，也不会扩大 grant。
- 自动 grant 不单独写一份 ApprovalDecision/ToolResult副本；正常 Session Tool Message 保留 ToolCall 与唯一 ToolResult，以满足 observation、协议回放和崩溃恢复。
- 取消 Task 或撤销 grant 立即阻止未来自动授权。任务仍可运行，但对应高级调用会被拒绝。

### 4.9 Attachment Copy 与 Update 详细执行边界

- `copy_attachment_to_workspace` 输入包含 opaque attachment ID、Workspace 相对目标路径、可选 `expected_sha256` 与显式 `create_parents`；不接受 Session ID、源文件路径或 Workspace root。
- 执行前后重新确认附件仍属当前 Session、源文件身份/hash、Workspace revision、目标真实路径与敏感路径规则。目标竞态返回 `FILE_CHANGED_SINCE_APPROVAL`。
- 未提供 `expected_sha256` 时只允许创建；提供时只覆盖 hash 匹配的普通文件。复制结果返回目标相对路径、字节数、源/目标 SHA-256 与是否覆盖。
- 原始附件可以是 PDF 等受支持上传类型，因此该 Tool 允许字节不变复制；它不允许模型提供任意二进制内容，也不解析或转换源文件。

- `write` 提交完整目标文本；创建模式校验目标不存在，覆盖模式校验 `expected_sha256`。
- `patch` 提交单文件补丁与预期 hash。服务端先完整 dry-apply，全部 hunk 成功后才形成候选内容与 diff。
- 从预览到执行期间再次读取文件身份/hash；变化后不套用旧候选，返回 `FILE_CHANGED_SINCE_APPROVAL` 供模型重读。
- 最终内容执行 UTF-8、大小、敏感资源和路径校验。`.git`、`.env`、`.env.*`受保护，`.env.example` 可写。
- 原子 replace 避免原地修改硬链接 inode；失败时保留旧文件并清理临时文件。
- 多文件修改由多个有序 ToolCall 完成，每个结果独立保存；不声称整个批次具有跨文件事务。

### 4.10 Shell 详细执行边界

- `ShellManager` 以受信任 Session ID 与 invocation ID 管理进程，Tool 参数不接受 shell ID、Session ID、invocation ID 或绝对 Workspace root。Gateway 重启后不恢复旧进程；`run_command` 返回 `SHELL_NOT_STARTED`。
- `new_shell` 由服务端选择当前平台固定的非交互解释器和启动参数。关闭旧 Shell 成功后才发布新 Shell；启动失败时不得保留半初始化句柄。
- `run_command` 接受一段明确 Shell 命令，可使用管道、重定向、变量和 `cd`，因此 Approval 卡展示原命令、当前 cwd、Workspace 与风险，用户不能在批准时改写参数。
- 命令协议使用不可碰撞的服务端 marker 划分本次 stdout/stderr，并读取退出码与执行后 cwd；marker 缺失、通道关闭或输出协议失步时终止 Shell，返回稳定错误。
- 基础环境 allowlist保留 PATH、系统目录和临时目录等运行必需项；服务端秘密、用户 Cookie 和 LLM 配置不被继承。Step 8 不接受模型直接注入初始环境变量，但命令在 Shell 内产生的环境变化可影响后续 `run_command`。
- stdout/stderr 按字节上限捕获，超限后继续有界排空到本次命令结束，并返回截断标志，避免管道阻塞。启动失败、非零退出、取消、超时和清理失败使用稳定错误码。
- Windows 使用隐藏窗口和 Job Object/等价进程树控制；其他平台使用进程组。停止、引导或超时会终结整个 Shell，不假设单条命令可安全撤销。
- AgentLoop 在 Run 终态前有界关闭 Shell；关闭失败进入审计，但不得把仍存活的进程报告为已终止。Workspace 变更仅允许无活动 Run，因此不会继承旧 Shell。
- cwd 检查只能约束 Shell 当前目录，不能阻止命令访问绝对路径或网络，也不能回滚副作用。

### 4.11 Download 详细执行边界

- Tool 只接受 Workspace 内相对普通文件路径，不接受目录、URL 或绝对路径。
- 服务端流式复制到内部 download 临时区，复制前后核对文件身份；文件变化时整次失败，不发布混合快照。
- random download ID、metadata、hash、大小和清理期限全部就绪后才发布入口。
- 十分钟内支持浏览器重试和有界 Range；入口不因首次成功提前失效，也不会重新读取已变化的源文件。
- 下载文件名清理后放入 `Content-Disposition: attachment`；HTML/SVG 等也以下载方式返回，避免同源内联执行。
- 访问次数不重复写入 Session/TaskStore；创建快照的 ToolResult 是唯一持久业务结果。

## 5. Step 9：Skill System

### 5.1 Registry 与快照

- 默认扫描本地 `skills/` 第一层目录。每个 Skill 必须有严格 UTF-8 `SKILL.md`，frontmatter 至少含 `name` 和 `description`，名称与目录一致。
- 未选择时只把 name、description、version/hash 的轻量索引放入上下文。
- 选择后加载完整 `SKILL.md`；资源通过只读 `read_skill_resource` 按需分段读取。
- Resource Tool 只读当前 Run 已选 Skill 的固定快照，保护绝对路径、`..`、符号链接和资源上限；不执行脚本或下载网络资源。
- Gateway 启动扫描一次，并提供 UI“刷新 Skills”和 CLI `/skill refresh`。
- 刷新先构建候选 snapshot，完成后原子切换。活动 Run 固定旧 snapshot，新 Run 使用 current snapshot。
- 单 Skill 损坏只标记该 Skill 不可用；全局扫描失败继续使用旧 snapshot。

### 5.2 选择规则

- 用户显式选择：在 Composer “+”菜单打开 Skill 列表；选中后在当前光标处插入 `$skill-name` mention。
- mention 的文字位置表达使用阶段，Runtime 仍在本轮首次 LLM 调用前加载全部显式 Skill。
- 删除 mention 即取消选择；Gateway 解析可见 mention并生成受信任 skill refs。成功入队后 Composer 清空 mention/附件选择。
- 单 Run 最多三个不同 Skill；重复选择幂等。第四个返回 `SKILL_LIMIT_REACHED`。
- 加载前进行 Token Budget 预检；超限返回 `SKILL_CONTEXT_BUDGET_EXCEEDED`，不截断 Skill。
- 模型可在普通聊天中自主选择 Skill。Runtime 显示 `SKILL_SELECTED` 名称和理由后立即加载，不等待用户批准。
- 自动选择告知不授予 Tool 权限；Skill 指令优先级低于 system/soul、安全和 Permission。
- 同一 Run 的引导沿用已选 Skill，总数不重置。

### 5.3 内置课堂 Skills

- `material-summary`：整理课堂笔记、阅读和调研材料，输出摘要/复习提纲，偏学习场景。
- `course-report`：生成课程论文、总结或实验报告 Markdown 草稿，偏作业场景；保存走 Update/Permission。
- `presentation-outline`：按主题、听众和时长生成逐页展示与讲稿大纲，不生成真实 PPT。
- description 同时写清适用与不适用场景；三个 Skill 可按顺序组合。

### 5.4 使用记录与 CLI

- SkillUsage 保存 Skill ID/版本、来源（`explicit / auto / scheduled_explicit`）、选择顺序、理由、时间、用户任务和输出/路径关联。
- UI 通知、完整 Skill 内容和重复 ToolResult 不复制进 SkillUsage。
- CLI 支持 `/skill list`、`/skill show <name>`、`/skill <name> <task>`、`/skill usage`、`/skill refresh`。

### 5.5 Skill 选择协议与上下文

- 普通请求的稳定上下文只包含有界轻量索引。模型通过 provider-independent 结构化选择提交 Skill 名称与简短理由，Runtime 不从自然语言回答猜测选择。
- Registry 验证规范名称、snapshot、可用状态和内容 hash。未知/损坏 Skill 返回结构化失败，不把名称当作磁盘路径。
- 显式 Skill 在首次 LLM 前加载；自动选择发生后，下一次 LLM 请求加入完整 Skill 指令层。
- Skill 指令不伪装成 User Message，也不修改 system prompt、soul 或 memory。Compaction 不改写已选 Skill；Token Budget 不足时拒绝新增。
- Skill selection 自身不算 Workspace/执行授权，但该模型响应仍属于 AgentLoop 的一个 LLM step，继续受总步骤和重复控制。
- 第四个不同 Skill 只拒绝新增选择，保留已加载 Skill 与当前 Run。重复选择同 Skill+版本不重复注入或新增 SkillUsage。

### 5.6 Skill resource 边界

- `read_skill_resource` 参数为已选 Skill ID、相对资源路径、读取上限和不透明 cursor。selected Skill集合来自 ToolExecutionContext。
- PathGuard 拒绝绝对路径、`..`、符号链接逃逸、敏感文件、目录和非普通文件。
- Step 9 资源只支持有界 UTF-8 文本。二进制模板、脚本执行、网络下载和依赖安装不属于本 Step。
- ToolResult 标明是否截断、剩余内容和 next cursor；结果作为普通 Tool Message进入历史和 Compaction。
- Skill 文件中任何“直接执行/无需批准”文字都不能改变 Tool safety level 或 PermissionPolicy。

### 5.7 Snapshot 刷新与恢复

- Refresh 在独立候选区枚举、解析、计算 hash 并生成 manifest；current Registry 在候选完成前保持不变。
- 单 Skill 格式错误记录为 unavailable；根目录不可读、规范化名称冲突、manifest 无法原子保存等全局错误拒绝整次切换。
- 已发布 snapshot 使用内容寻址缓存；`read_skill_resource` 从固定缓存读取，不回到正在编辑的源目录。
- 活动 Run、PendingApproval、Task skill ref 和保留的 SkillUsage引用具体 snapshot。仍有引用的 snapshot 不清理。
- Gateway 重启恢复安全 checkpoint 时继续使用原 snapshot；缓存缺失/损坏则终结为 `SKILL_SNAPSHOT_UNAVAILABLE`，不换用最新版。
- Refresh API 是带本地认证与严格 Origin 的状态修改操作；扫描过程不执行任何 Skill 内容。

### 5.8 Composer mention 详细行为

- “+”弹层同时提供附件和 Skill。Skill 列表展示名称、description、版本、可用状态、不可用原因和刷新入口。
- 选择后在当前 caret插入可见 `$skill-name` mention；删除 token 即取消。附件 chip 与 Skill mention 保持不同视觉语义。
- mention 在句子中的位置帮助模型理解阶段，例如“先整理材料，再用 `$course-report` 写报告”；Runtime 仍在首次调用前预加载显式 Skills。
- Gateway 根据明确词法边界解析 mention，生成受信任 skill refs，并提供转义语法用于把 `$skill-name` 当普通文本讨论。
- 前端提交可见 Composer 文本和附件 refs，服务端校验 current snapshot、数量、hash 与 Session。任一不一致整批拒绝，不静默换版本。
- 编辑器重点支持中文 IME、caret移动、复制粘贴、撤销/重做、屏幕阅读器、Escape 与焦点恢复。
- 自动选择的 Skill 只显示在当前回答旁，不回填 Composer mention。

### 5.9 三个 Skill 的选择边界

- `material-summary` description强调材料提取、学习摘要与忠实性；不承担完整论文论证。
- `course-report` description强调课程要求、字数、结构化 Markdown 与保存路径；不替代材料事实核验。
- `presentation-outline` description强调听众、时长、逐页结构、讲稿与时间分配；不生成论文或二进制 PPT。
- 三者可以按 `material-summary -> course-report -> presentation-outline` 组合；每个选择来源、版本和顺序分别记录。
- 每个 Skill 至少有 SKILL.md，可附有有界模板或检查清单；固定资源不包含现成课程答案、未经核验引用或可执行脚本。
- 验收覆盖三个显式调用，以及至少一次受控/真实模型自动选择能区分相近课堂请求。

## 6. 跨模块合同

### 6.1 Run 状态机

```text
RECEIVED -> PERSISTED_PENDING -> QUEUED
QUEUED -> RUN_STARTED -> STREAMING / TOOL_RUNNING / WAITING_APPROVAL
WAITING_APPROVAL -> QUEUED_FOR_RESUME -> TOOL_RUNNING
RUN_STARTED -> COMPLETED / FAILED / CANCELLED / INTERRUPTED
QUEUED -> CANCELLED_BEFORE_START
```

- `PERSISTED_PENDING` 是确认接收边界；此前失败不形成队列项，此后重试返回同一请求。
- `WAITING_APPROVAL` 保留 Session 活动所有权并释放全局许可；恢复后重新公平排队，不越过同 Session 后续请求。
- ToolCall、Approval、ToolResult 与 Assistant Message按各自边界即时保存；终态不会删除已提交 ToolResult或掩盖真实/未知副作用。
- RuntimeHost 终态恰好一次。取消、停止、引导、审批、Occurrence 提交和 SSE 重连都用稳定 ID 幂等。

### 6.2 ToolExecutionContext

```text
session_id
request_id / invocation_id
source / actor_id
workspace_binding_id / revision / root
selected_skill_snapshot_ids
scheduler_task_id / occurrence_id（如适用）
managed_permission_categories（如适用）
```

这些字段由 RuntimeHost/Session 构建。Tool schema 只暴露相对路径、命令和业务参数；模型不能提供受信任 root、Session、grant 或 Skill根。

### 6.3 Permission 决策

- 普通 Gateway/CLI：READ_ONLY 默认 allow；WRITE/EXECUTE/Download按策略 allow、ask、deny，ask进入 PendingApproval。
- Scheduler：READ_ONLY 默认 allow；高级类别同时匹配 Task grant 与 Workspace revision 才 allow，否则 deny。
- `copy_attachment_to_workspace` 是 WRITE，使用普通聊天的逐次 Approval 或 Scheduler 当前 Task 的“Workspace 写入”授权。
- `new_shell` 与 `run_command` 都是 EXECUTE，普通聊天逐个 ToolCall 审批；Scheduler 的“Shell 命令”类别同时覆盖二者。
- Skill 只改变上下文，不改变 Permission source。
- 未注册 Tool、schema 错误、越界、敏感路径、超时硬上限、重复调用和最大步数不受 Approval 覆盖。

### 6.4 Advanced Tool 接口

所有 Tool 继续使用 Goal 5 的严格 ToolDefinition、ToolCall、ToolIntent、AuthorizedToolCall 与 ToolResult。模型只提供业务参数；Session、invocation、Workspace root/revision 和 Permission grant 来自 ToolExecutionContext。

`copy_attachment_to_workspace` 输入是严格对象：

```json
{
  "attachment_id": "opaque-id",
  "destination": "materials/source.pdf",
  "expected_sha256": null,
  "create_parents": true
}
```

- `attachment_id` 与 `destination` 必填；`expected_sha256` 可省略或为 `null`，表示仅创建，提供 hash 才允许覆盖；`create_parents` 默认 false。
- 结果包含 attachment ID、目标相对路径、字节数、SHA-256 和 `overwritten`。任何结果都不暴露附件存储路径或 Workspace 绝对路径。

`new_shell` 输入是严格对象：

```json
{"cwd": "."}
```

- `cwd` 可省略，默认 Workspace 根；存在时必须是 Workspace 内相对目录。拒绝未知字段、绝对路径和路径逃逸。
- 成功结果包含清理后的 cwd、Workspace revision 和 `replaced_existing`；不向模型或 UI 暴露 PID、管道句柄或物理管理信息。

`run_command` 输入是严格对象：

```json
{"command": "py -m pytest -q", "timeout_seconds": 120}
```

- `command` 必填、非空并受字符上限限制；`timeout_seconds` 可省略，只能在 `1..SHELL_MAX_TIMEOUT_SECONDS` 内。
- 成功或失败结果均包含清理后的 command、`cwd_before`、`cwd_after`、`exit_code`、stdout、stderr、`timed_out` 和 `truncated`；不适用字段使用 `null`，不用缺失字段表达状态。
- 同一模型批次同时包含 `new_shell` 和 `run_command` 时，ToolRegistry 先做既有整批结构/schema 校验；通过后 AgentLoop 保持原顺序，逐个完成上下文校验、Permission 与执行。前一个失败或被拒绝时，后一个在申请 Approval 前生成 `SHELL_NOT_STARTED` ToolResult。
- 稳定错误至少包括 `SHELL_NOT_STARTED`、`SHELL_LEFT_WORKSPACE`、`SHELL_START_FAILED`、`SHELL_PROTOCOL_ERROR` 和通用 `TOOL_TIMEOUT`；safe message 不包含 PID、秘密环境或 Workspace 绝对路径。
- ToolCall、Approval 与 ToolResult 按既有 Message 协议即时保存。实时 SSE 只发布清理后的 Tool 名称、状态和有界摘要，完整受限输出以 Session Tool Message 为事实。

Gateway 不提供绕过 AgentLoop 的 Update、Attachment Copy、Shell 或 Download 创建 endpoint。网页和 CLI 只能提交用户请求、观察 Tool 事件并通过 Approval endpoint 决策；`GET /downloads/{download_id}` 只读取已经由 Tool 创建的不可变快照。

### 6.5 REST API 面

Gateway 使用 `/api/v1`：

```text
POST   /api/v1/auth/bootstrap
GET    /api/v1/sessions
POST   /api/v1/sessions
GET    /api/v1/sessions/{session_id}
POST   /api/v1/sessions/{session_id}/requests
DELETE /api/v1/sessions/{session_id}/requests/{request_id}
POST   /api/v1/sessions/{session_id}/runs/{run_id}/stop
POST   /api/v1/sessions/{session_id}/runs/{run_id}/steer
POST   /api/v1/sessions/{session_id}/runs/{run_id}/approvals/{approval_id}/decision
GET    /api/v1/runs/{run_id}/events

GET    /api/v1/sessions/{session_id}/attachments
POST   /api/v1/sessions/{session_id}/attachments
DELETE /api/v1/sessions/{session_id}/attachments/{attachment_id}

GET    /api/v1/tasks
POST   /api/v1/tasks
GET    /api/v1/tasks/{task_id}
GET    /api/v1/tasks/{task_id}/occurrences
POST   /api/v1/tasks/{task_id}/cancel
POST   /api/v1/tasks/{task_id}/occurrences/{occurrence_id}/retry

PUT    /api/v1/sessions/{session_id}/workspace
DELETE /api/v1/sessions/{session_id}/workspace
GET    /api/v1/downloads/{download_id}

GET    /api/v1/skills
GET    /api/v1/skills/{skill_id}
POST   /api/v1/skills/refresh
```

- Approval decision endpoint 使用严格 `oneOf` payload：

```json
{"decision": "approve", "idempotencyKey": "opaque-key"}
```

```json
{"decision": "deny", "reason": "目标路径不正确，请先重新读取文件。", "idempotencyKey": "opaque-key"}
```

- `reason` 仅在 `deny` 时可选；空白规范化后视为未提供，`approve` 携带 reason 或出现未知字段时整单拒绝。响应返回持久化后的 Approval 状态和关联 Run ID，不回显未清理文本。
- 路由文件可按领域拆分，但 UI 组件不创建重复业务入口。
- 创建与控制请求携带幂等 key；服务端生成稳定 ID，客户端不指定路径型 ID。
- 列表使用稳定排序、page size 上限和 opaque cursor；客户端不能传 Store 路径或任意文件 offset。

### 6.6 REST 错误 envelope

```json
{
  "error": {
    "code": "STABLE_ERROR_CODE",
    "message": "safe user-facing message",
    "requestId": "request-id",
    "details": {}
  }
}
```

HTTP status表达错误大类，`code`表达可测试领域原因。响应排除 traceback、API key、敏感绝对路径、原始 provider body 和未清理命令环境。安全语义字段使用严格 schema，未知字段不静默忽略。

### 6.7 应用数据布局

```text
<data_dir>/
  sessions/<session_id>.json
  memory.json
  attachments/<session_id>/<attachment_id>/
  scheduler/tasks/<task_id>.json
  scheduler/occurrences/<task_id>/<sequence>-<occurrence_id>.json
  skill-snapshots/<snapshot_id>/
  downloads/<download_id>/
```

- AttachmentStore管理 original、normalized text 和 metadata；Session只存引用。
- SkillSnapshotStore管理 manifest 与受限内容缓存；源 `SKILL_ROOT` 与 snapshot 目录分离。
- Download区是临时数据，启动时清理过期/孤立快照。
- Shell 进程、cwd 和环境变量属于 `ShellManager` 的易失运行态，不写入 JSON；Session 只保存 ToolCall、Approval、ToolResult 和 Run 审计。
- 目录名只使用服务端 ID，校验路径分隔符、Windows保留名、大小写与 Unicode规范化碰撞。
- 原子写使用同文件系统 temp + replace；Windows占用导致失败时保留旧版本并返回 Store 错误。
- 启动时检测同一 data_dir 的第二个 writer 并失败，避免最后写入者覆盖。

## 7. 关键默认配置

```text
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=8765
GATEWAY_BOOTSTRAP_TIMEOUT_SECONDS=300
GATEWAY_SSE_REPLAY_EVENTS=1000
GATEWAY_SSE_HEARTBEAT_SECONDS=15
RUNTIME_MAX_CONCURRENT_RUNS=2

LLM_CONNECT_TIMEOUT_SECONDS=10
LLM_STREAM_IDLE_TIMEOUT_SECONDS=60
LLM_TOTAL_TIMEOUT_SECONDS=600

ATTACHMENT_PROCESS_TIMEOUT_SECONDS=60
APPROVAL_TIMEOUT_SECONDS=1800
APPROVAL_REASON_MAX_CHARS=1000
UPDATE_MAX_FILE_BYTES=2097152
UPDATE_TIMEOUT_SECONDS=30
SHELL_DEFAULT_TIMEOUT_SECONDS=120
SHELL_MAX_TIMEOUT_SECONDS=900
SHELL_START_TIMEOUT_SECONDS=10
SHELL_CLEANUP_TIMEOUT_SECONDS=10
SHELL_COMMAND_MAX_CHARS=65536
SHELL_OUTPUT_MAX_BYTES=1048576
DOWNLOAD_MAX_FILE_BYTES=104857600
DOWNLOAD_TTL_SECONDS=600

SCHEDULER_MIN_INTERVAL_SECONDS=300
SKILL_MAX_PER_RUN=3
SKILL_MAX_COUNT=100
SKILL_MAX_RESOURCE_FILE_BYTES=1048576
SKILL_MAX_TOTAL_SNAPSHOT_BYTES=10485760
SKILL_RESOURCE_READ_MAX_CHARS=32000
SKILL_SCAN_TIMEOUT_SECONDS=10
```

模型和客户端只能请求更小的可选上限。非法配置在 Bootstrap 阶段失败。所有阻塞操作使用可取消异步等待或可终止子进程。

## 8. 验收

- Step 6：真实流式、SSE 重连、同源认证、安全 Markdown、Session/队列/引导/停止、附件隔离与单轮计时通过确定性测试。
- Step 7：四类 schedule、虚拟时钟、最近 deadline 动态等待、新建更早 Task 立即唤醒、合并补执行、失败续期、不重叠、取消恢复、Task/Occurrence Store 与任务 UI 通过。
- Step 8：Workspace 竞态、PathGuard、附件原样复制、Update、持久 Shell 生命周期与状态复用、Download、普通 Approval 的拒绝原因/幂等/恢复/超时及 Scheduler 托管权限通过。
- Step 9：扫描/刷新快照、光标 mention、三 Skill 上限、自动选择告知、按需资源、三个课堂 Skill、SkillUsage 与 CLI 通过。
- 所有入口证明复用 RuntimeHost/AgentLoop；默认测试不联网、不加载真实 `.env`。

```text
py -m pytest -q
py -m mypy src
py -m ruff check .
py -m compileall -q src
npm ci
npm run lint
npm run test -- --run
npm run build
```

关键 Gateway 流程增加最小浏览器 E2E；真实 DeepSeek streaming/Tool/Skill 路径单独标记 live。最终 UI 点击、视觉、响应式与无障碍细节的调试和确认由用户在本地浏览器手动完成；实施代理提供启动步骤、预期现象和排查路径，不以穷举式模拟点击调试作为完成条件，也不把未执行的手工 UI 检查声明为已验证。

## 9. 非目标与已知风险

- 非目标：公网/局域网、多用户、机器人入口、WebSocket、reasoning 展示、OCR/Word/多媒体、cron、Task 原地编辑、文件删除、任意二进制生成/编辑、交互式终端、远程 Skill 安装、真实 PPT、Step 10 SQLite 实现。`copy_attachment_to_workspace` 仅按字节复制现有附件，不属于任意二进制写入。
- Shell cwd 不是 OS 沙箱；Task 级永久 Shell 类别授权范围较宽。
- 课程示例允许 `create_download` 不做显式 Approval；本设计在创建不可变下载快照前仍要求普通 Approval 或 Task grant，用户点击链接后才读取快照。该策略更严格，答辩时需说明。
- 自动 Skill 选择采用告知而非阻塞批准，答辩时需说明对课程“approval 信息”的解释。
- 内联 Skill mention 需要重点验证中文 IME、Selection、粘贴、撤销和无障碍。
- JSON 跨文件流程依靠稳定 ID、幂等状态机和启动对账；Occurrence/Skill snapshot 历史依靠分页和内容寻址控制增长。

## 10. Step 10 迁移边界

未来 SQLite 按 Session、Message、Run、PendingRequest、Attachment、Task、Occurrence、WorkspaceBinding、ApprovalDecision、SkillSnapshot、SkillUsage 建表。迁移器通过旧 Store reader 读取受支持 JSON，写入临时数据库，校验数量/外键/hash 后原子切换，并保留原 JSON 备份。

## 11. 中期报告素材与提交准备

当前不写报告正文。每完成一个 Step checkpoint，按 `SJTUClaw.md` 的 Step 编号和课堂验收用语补一个几十字条目：

```text
Step N（原则上不超过 100 字）：课堂目标；当前完成；一句验证证据；不足或下一步。
```

- 每个 Step 可写两三句，省略文件清单、排错过程和完整日志。
- “已实现”以代码、commit 和新鲜测试为准；设计未落地标为“计划中”。
- 最终报告不超过 1000 字，按“已实现功能—模块与数据结构—未完成和计划”三段组织。
- 中期 ZIP 从受控清单生成，排除真实 `.env`/API key、运行数据、附件、下载快照、缓存、虚拟环境和 `node_modules`；保留 `.env.example` 与构建说明。
- ZIP 完成后列出归档内容并扫描秘密，确认源码、测试、文档和报告齐全。

## 12. 后续规则

后续设计变更继续同步到本 spec。下一任务生成 implementation plan，不在计划阶段提前写实现代码。
