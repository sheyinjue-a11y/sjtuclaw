# SJTUClaw Step 6–9 实施计划（已弃用）

> 状态：已弃用，仅保留为历史记录。现行行为与约束以对应 spec 和当前代码、测试为准。

**规格：** `docs/superpowers/specs/2026-07-19-sjtuclaw-step6-9-design.md`

**目标：** 在已完成的 CLI、JSON Store 和 Agent Loop 之上，交付单机同源 Gateway、可恢复 Scheduler、受审批的 Workspace 高级 Tool，以及可审计的本地 Skill 系统。

**架构：** 将现有 `AgentRuntime` 的入口职责演进为持久 `RuntimeHost`；FastAPI、CLI 和 Scheduler 只提交 `RuntimeRequest` 或控制命令，共用 Session、Context、Compaction、ToolRegistry 和 AgentLoop。React/Vite 由 FastAPI 同源托管，所有 JSON 写入继续经 Store port。

**全局约束：** 只实现获批 Step 6–9；JSON 是唯一生产后端和单进程 writer，Step 10 仅保持迁移模型、不实现 SQLite 或双写。Gateway 仅监听 loopback，浏览器只经 bootstrap Cookie 访问；模型、附件、Skill 和 Tool 输出均视为不可信。绝不读取、输出、修改或提交 `.env`/密钥，也不修改、暂存 `instruction_doc.md` 或 `SJTUClaw.md`。自动化只保留关键 Gateway 协议的最小浏览器 E2E；最终 UI 点击、视觉、响应式和无障碍调试由用户手工完成，实施代理只交付操作步骤、预期和排查，不能把未做的手工检查说成已验证。不实现公网/多用户/WebSocket、OCR 或多媒体、cron、Task 原地编辑、文件删除、交互终端、远程 Skill 安装、真实 PPT 或任意二进制生成。

## 目标 1：建立 Step 6 的可恢复 Runtime、流式与附件基础

**规格依据：** §1.1–§1.3，§2.3、§2.5–§2.12、§2.16–§2.17，§6.1–§6.4、§6.7，§7。

**目标结果：** `RuntimeHost` 能以 Session 串行、跨 Session 公平且最多两个并发 Run 的方式，持久接受、恢复、取消和引导请求；DeepSeek 的真实流式增量、附件和安全 Tool 生命周期在同一事件/持久化边界内工作。

**范围：**

- 将现有 Session JSON v2 升至 v3：加入 `PendingRequest`、附件引用、Run 生命周期、`durationMs` 与 `assistantMessageId`；新增有版本的 Attachment metadata/Store 和原文件、normalized text 的安全目录布局。Reader 严格支持既有 v1/v2 及新 v3，writer 仅写当前版本，首次正常保存原子升级；未知未来版本或损坏内容保持拒绝且不覆盖。
- 扩展 `RuntimeRequest`、Run 状态/记录、`RuntimeEvent` 与 Tool 执行上下文，使 request/run/steer/attachment/持续时间/安全进度具备稳定 ID、UTC 时间、幂等键和持久化边界；所有入口只经 `RuntimeHost` 与 Store port 写入。
- 实现 Session 原子 `PendingRequest -> User Message + RUN_STARTED`、队列取消、停止、每 Run 最多五次引导、安全点处理、Gateway 重启对账和公平恢复；中断 Run 保留已提交用户消息而不重放 LLM/Tool。
- 为 DeepSeek Adapter 增加真正 streaming 的 provider-independent `ASSISTANT_DELTA`，在内存分别组装文本、`reasoning_content` 与完整 Tool fragments；只在成功结束保存完整 Assistant Message，且不让 reasoning、未完成参数、原始错误或秘密越过内部边界。
- 实现上传/提取/取消/删除与 `read_attachment`：流式大小与 SHA-256、临时文件原子发布、严格 UTF-8/PDF 文本层限制、Session 归属与引用竞态检查。落实已批准的大小、页数、字符数、读取和超时上限，不让模型传路径或 Session ID。
- 以 `FakeClock`、确定性 ID、Fake streaming LLM 和临时数据目录验证 v2→v3 升级、幂等接受、恢复/取消/停止/引导竞争、公平并发、完整终态恰好一次、流中断不留 partial、附件边界/清理和超时映射。

**依赖：** 已完成的 Goal 0–5 Runtime、Session/Memory Store、严格 Tool 协议和 PermissionPolicy。

**验收：** 断电/重启模拟后，已持久 PendingRequest 按原顺序只恢复一次，已活动 Run 记录 `RUN_INTERRUPTED_GATEWAY_RESTART` 而不重放；两个 Session 在全局许可内公平推进，同一 Session 绝不并行。Fake streaming 可复现 delta、Tool fragment、取消与终态顺序；附件无法跨 Session/越界读取或在引用后删除，历史中不出现未完成回答或 `reasoning_content`。

**检查点：** `feat: add durable runtime streaming and attachments`

## 目标 2：交付同源 Gateway、React 工作台与 Step 6 浏览器合同

**规格依据：** §2.1–§2.4、§2.13–§2.15，§6.5–§6.6，§7–§8。

**目标结果：** 一个 loopback FastAPI 进程可同源提供已构建 React/Vite 工作台、Step 6 REST/SSE、Cookie 认证、安全 Markdown 和完整聊天/附件/队列体验。

**范围：**

- 新增 Gateway composition/route 层及前端工作区；锁定 React、TypeScript、Vite 与本地依赖，不使用 CDN。Bootstrap token 只经 URL fragment 兑换一次，随即清除；Cookie、Host、严格同源 Origin、CSP 和统一 `code + safe message + requestId + details` envelope 保护业务 API 与 SSE。
- 发布 v1 的 auth、Session、request、run 控制、event 和 attachment 路由；后续 Task、Workspace/Approval、Download 与 Skill 路由沿 §6.5 由目标 3–5 加入，且不提供绕过 AgentLoop 的高级 Tool endpoint。
- 把 RuntimeHost 的快照/增量映射为有递增事件 ID 的 SSE：首次快照、`Last-Event-ID` replay、缓存过期回读、heartbeat、终态一次且最后发送；网络断开只影响观察，不取消 Run。
- 完成三栏工作台、响应式抽屉和可访问焦点管理；实现明确 Session 选择、新建、Composer、附件状态、排队/停止/引导、受服务端时长校正的计时和稳定状态码到中文文案的映射。流式/终态 Markdown 都走禁用 raw HTML 的清理渲染链路。
- 以前端单元/组件测试覆盖状态、Composer、计时和安全渲染；以最小浏览器 E2E 覆盖 bootstrap、Cookie/Origin 拒绝和 SSE 重连/缓存缺口。Session 路由、真实流展示、附件、抽屉、窄屏焦点与视觉点击细节交由用户作最终手工调试。

**依赖：** 目标 1 的 RuntimeHost、流式事件、附件 Store 与持久队列。

**验收：** 启动 Gateway 后仅需一个本地 URL；fragment 被清除、重启后旧 Cookie 失效，跨 Origin/错误 Host 不能调用状态修改或 SSE。最小浏览器 E2E 证明刷新或断线不会取消 Run/拼接残缺文本；恶意 Markdown、链接 scheme 与附件文本不能执行脚本。最终工作台点击与视觉表现由用户按交付步骤手工确认。

**检查点：** `feat: add local gateway and React workspace`

## 目标 3：实现可恢复的 Scheduler、Task/Occurrence Store 与任务界面

**规格依据：** §3.1–§3.8，§6.1–§6.3、§6.5–§6.7，§7–§8。

**目标结果：** Scheduler 在同一 RuntimeHost 中以确定性、可恢复方式运行 `once`、`interval`、`daily`、`weekly` 任务，并通过 Gateway 工作台管理其不可变历史。

**范围：**

- 增加有独立 schema 的 Task、Schedule 和 Occurrence 模型/Store，数据落在 `scheduler/tasks/<task_id>.json` 与 `scheduler/occurrences/<task_id>/<sequence>-<occurrence_id>.json`；实现严格校验、路径/ID 防护、原子替换、cursor 历史、受支持旧版本 reader 和当前 writer。Occurrence 只关联 Run/Assistant Message，不复制正文或 ToolResult。
- 用可注入 Clock、稳定 anchor 和最近 deadline/wakeup 驱动服务端调度，实行最小 300 秒 interval、UTC/IANA 规则、补执行合并、失败续期、重叠跳过、一次性重试，以及持久化后再以 occurrence 派生幂等键提交 RuntimeRequest 的启动对账。
- 实现 `CANCEL_REQUESTED -> CANCELLED` 与 RuntimeHost 队列锁的竞态语义；取消未来/未开始 Occurrence、保留活动 Run 真实结果、重启后不重复提交。损坏单个 Task/Occurrence 只停止其调度且不覆盖文件。
- 增加 `/api/v1/tasks` 的创建、列表、详情、历史、取消和重试接口及任务视图。表单/详情提供 Session、规则、时区、服务端 next run、筛选、分页和到 Run/Message 的跳转；浏览器绝不自行计时触发 Agent。
- 为获批的跨 Step 合同持久 `task_id + session_id + workspace_binding_revision`、0–3 个固定 Skill refs 和三类 managed permission。此时零 Skill、只读任务完整可用；目标 4/5 分别为同一字段接入 Workspace grant 与已验证的 Skill snapshot，避免平行权限或独立 Agent Loop。
- 用虚拟时钟和临时 JSON Store 覆盖四类计划、无 sleep 的 next-run、停机合并、锚点不漂移、失败/重叠/取消竞争、幂等恢复、cursor 与损坏隔离；以 Gateway API 集成测试覆盖任务创建、预览、历史、取消和重试，最终页面点击由用户手工确认。

**依赖：** 目标 1 的持久 RuntimeHost/公平队列，目标 2 的 Gateway 与前端基础。

**验收：** 同一 Task 永不积压多条活动/排队 Occurrence；重启后只形成一次合并补执行，周期任务从原 anchor 计算下一次。任务取消与 Run 开始的先后产生已定义的不同结果，历史可分页访问且没有完整回答副本；所有触发在 RuntimeHost 的并发上限内执行。

**检查点：** `feat: add scheduler and task management`

## 目标 4：完成 Session v4 Workspace、高级 Tool、Approval 与下载

**规格依据：** §4.1–§4.11，§6.1–§6.4、§6.7，§7–§8。

**目标结果：** 每个 Session 可安全绑定一个 Workspace；写入、Shell 和下载请求通过可恢复的逐次 Approval 或正确的 Scheduler grant 执行，且工具副作用和恢复状态真实可审计。

**范围：**

- 将 Session 升至 v4，持久 WorkspaceBinding/revision、PendingApproval、ApprovalDecision 和可恢复 Run checkpoint；继续严格读取 v1–v3、只写 v4 并在正常保存时原子升级。扩展 `ToolExecutionContext` 和 PermissionPolicy，使可信 Session/Workspace/Task grant/Snapshot 来源不由模型参数提供。
- 实现 WorkspaceService 与统一 PathGuard，在 Session 协调锁内绑定、替换、解除与审计；严格执行 `WORKSPACE_BUSY`、revision、真实路径身份、符号链接、`.git` 和 `.env` 边界，且高级 Tool 不回退到 `TOOL_ROOT`。
- 实现持久 Approval 状态机、30 分钟超时、决定幂等/冲突、CLI 交互决定和 Gateway decision endpoint。等待审批时释放全局许可但不失去 Session 顺序；重启后恢复 checkpoint，已批准却未保存结果的副作用 Tool 不自动重跑。
- 以既有 `ToolDefinition -> ToolIntent -> AuthorizedToolCall -> ToolResult` 链路注册并执行 `copy_attachment_to_workspace`、严格 `write / patch` Update、`new_shell`、`run_command` 与 Download。复制/Update 使用预期 hash、临时文件和原子 replace；Shell 只在单 Run/Session 中保留，非交互、秘密环境隔离、cwd/revision 复核、可终止进程树和有界输出；Download 只发布有 TTL 的不可变快照。
- 激活目标 3 中的 Scheduler Workspace/Shell/Download 类别 grant：仅 task/session/revision 三者相符才自动允许，未授权返回 `SCHEDULED_PERMISSION_DENIED`，不写虚假的逐次 Approval。
- 用确定性并发、FakeClock、临时 Workspace/附件和受控子进程测试绑定竞态、PathGuard、copy hash、Update conflict、approval 批次顺序/拒绝原因/超时/重启、Shell cwd/复用/清理/超时、snapshot 过期和 managed grant；以 Gateway API/组件测试覆盖审批、下载与 Workspace 忙碌反馈，最终审批卡点击由用户手工确认。

**依赖：** 目标 1 的附件/Run checkpoint，目标 3 的 Task grant 存储与调度入口。

**验收：** Workspace 变更不能越过已接受请求或活动 Run；一个 ask ToolCall 的批准不授权其他调用。文件竞态生成 `FILE_CHANGED_SINCE_APPROVAL` 而不覆盖，copy 保持原附件字节；`run_command` 在同一 Shell 保留 cwd/环境且任一终态或协议错误会停止进程树；下载重试始终读取同一快照而非变化后的源文件。

**检查点：** `feat: add workspace tools and recoverable approvals`

## 目标 5：完成 Session v5 Skill snapshot、选择与 mention 体验

**规格依据：** §5.1–§5.9，§6.1–§6.3、§6.5、§6.7，§7–§8。

**目标结果：** 本地 Skill 可安全扫描、原子刷新和内容寻址快照；显式 mention、模型自动选择和 Scheduler 固定版本都能以受限上下文资源、稳定顺序和可审计 Usage 服务于同一 AgentLoop。

**范围：**

- 将 Session 升至 v5，保存显式 Skill refs、`SkillUsage`、snapshot/hash 与选择顺序；增加 Skill manifest/snapshot Store、`skills/` 扫描和内容寻址受限缓存。reader 支持 v1–v4，writer 只写 v5；活动 Run、PendingApproval、Task refs 和历史 Usage 保持具体 snapshot，缺失快照终结为 `SKILL_SNAPSHOT_UNAVAILABLE` 而不静默换新版。
- 实现严格 `SKILL.md` frontmatter、单 Skill unavailable 与全局扫描失败回退旧 snapshot、原子 refresh、数量/字节/读取上限以及 `read_skill_resource`。资源只从当前 Run 已选快照以相对路径分段读取，拒绝脚本执行、网络、二进制、绝对/逃逸/符号链接路径。
- 扩展 ContextBuilder、RuntimeHost 与 AgentLoop：未选时仅注入轻量索引；首次 LLM 前加载可信显式 refs；自动选择使用 provider-independent 结构化协议，显示理由但不增加 Permission。强制每 Run 至多三个不同 Skill、Token 预算预检、引导继承、重复选择幂等及完整 Usage 记录。
- 提供 `material-summary`、`course-report`、`presentation-outline` 的边界清晰 `SKILL.md`/有界资源；实现 CLI `/skill list|show|<name> <task>|usage|refresh`、Gateway skills 路由和 Composer “+”菜单/词法 `$skill-name` mention、转义、IME/键盘/无障碍行为。令目标 3 的 Task 固定 refs 在创建与运行时对 current/指定 snapshot 严格验证。
- 用确定性临时 Skill 根、Fake LLM/Clock/ID 覆盖扫描/损坏/刷新原子性、v4→v5、快照保留、资源限制、三个上限、显式/自动/定时来源、上下文顺序、预算拒绝和 mention 解析；组件测试覆盖可访问状态和词法 mention 行为，中文 IME、caret、粘贴、撤销、删除 mention 与刷新交互由用户手工确认。

**依赖：** 目标 1 的稳定 Context/Run 生命周期、目标 2 的 Composer、目标 3 的 Task refs、目标 4 的受信任 ToolExecutionContext。

**验收：** 正在执行的 Run 在 refresh 后仍读取旧 snapshot，新 Run 才使用新 snapshot；坏 Skill 不使可用 Skill 消失，扫描全局失败不替换 current。第四个不同 Skill 和超预算请求在调用模型前稳定拒绝；Skill 文本不能扩大 Tool 权限。三个课堂 Skill 的显式路径和一次可控的自动选择都产生带版本/顺序/来源的 Usage，定时任务只使用固定版本。

**检查点：** `feat: add versioned local skill system`

## 目标 6：完成全链路验证、真实 DeepSeek、文档与交付证据

**规格依据：** §8–§12。

**目标结果：** Step 6–9 的离线确定性测试、前端构建/最小 E2E、受控 live DeepSeek 验证和用户手工 UI 验收说明形成可重复、可安全回滚的交付证据。

**范围：**

- 在不联网且不加载真实 `.env` 的默认测试集中覆盖所有 Store schema/migration、RuntimeHost、Gateway/SSE、附件、Scheduler、Workspace/Approval/高级 Tool、Skill 的确定性合同；保持 JSON Store 契约与文件损坏不覆盖的负例。
- 按 §8 执行 Python 检查（`py -m pytest -q`、`py -m mypy src`、`py -m ruff check .`、`py -m compileall -q src`）以及前端 `npm ci`、lint、unit test 和 production build，并运行关键 Gateway 协议的最小浏览器 E2E。
- 为真实 DeepSeek streaming、Tool 和 Skill 路径保留单独 `live` 标记；只在用户已存在的本地配置下、使用无敏感提示词和隔离临时数据目录顺序执行，绝不打印 key、Authorization 或 provider 原始 body。将不可稳定复现的队列/竞态/超时继续留给 Fake 测试。
- 更新 README、验收指南、配置说明和中期报告素材，使每个 Step 仅陈述已由新鲜测试/commit 证明的功能，列出由用户执行的最终 UI 点击/视觉/响应式/无障碍验收步骤、预期现象与排查路径。按目标检查点只暂存本里程碑文件，保留用户现有脏状态和所有秘密/运行数据在提交外。

**依赖：** 目标 1–5 全部完成。

**验收：** 全部离线质量命令、前端构建和最小 E2E 通过；live 记录显示真实流、Tool 和 Skill 闭环已在隔离数据目录完成，未泄露秘密。文档能让用户逐项手工复测 Gateway、任务、Approval、Download 和 Skill 的 UI 点击与视觉表现，且不把这项用户验收误报为自动化结果；提交前检查确认 `.env`、`instruction_doc.md`、`SJTUClaw.md`、运行附件/快照/缓存和既有脏改动未被纳入。

**检查点：** `test: validate Step 6-9 end-to-end and document acceptance`

## 规格覆盖检查

- 总体架构、版本、共享 Run/Tool/Store 合同：目标 1，并由目标 3–5 按 Session v3/v4/v5 继续演进。
- Step 6：目标 1（持久 Runtime、streaming、附件、队列/引导/恢复）与目标 2（Gateway、SSE、认证、工作台与浏览器 E2E）。
- Step 7：目标 3；其 Workspace grant 和固定 Skill ref 在目标 4、5 激活，仍使用同一持久 Task 合同。
- Step 8：目标 4，包括 `copy_attachment_to_workspace`、Update、持久 `new_shell`/`run_command`、Approval 与 Download。
- Step 9：目标 5，包括 snapshot、SkillUsage、自动/显式选择、resource、mention、CLI 和三个课堂 Skill。
- 默认配置、测试、非目标/安全边界、Step 10 仅迁移模型、报告和提交：目标 1–6 中按适用范围落实，最终由目标 6 汇总验证。
