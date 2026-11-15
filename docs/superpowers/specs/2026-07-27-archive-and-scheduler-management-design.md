# 归档与定时任务管理设计

日期：2026-07-27  
状态：已获用户批准，等待书面复核

## 1. 目标

本次改动解决两类管理问题：

1. 定时任务页面受全局 `overflow: hidden` 影响，内容超出视口后无法滚动。
2. 定时任务和会话缺少统一的搜索、筛选、编辑、归档、恢复与永久删除流程。

最终采用“软删除进入归档，归档区内才能永久删除”的统一交互。定时任务和会话均保留可恢复的历史；永久删除是显式、二次确认且不可恢复的操作。

## 2. 范围

### 2.1 包含

- 定时任务页面纵向滚动。
- 普通定时任务列表、搜索、状态筛选、选中与详情。
- 定时任务编辑；编辑时终止该任务排队和运行中的执行。
- 取消任务后自动归档。
- 已归档任务列表、搜索、恢复和永久删除。
- 删除会话改为归档会话。
- 归档会话时终止排队、运行、等待审批的请求。
- 归档会话时级联取消并归档其关联定时任务。
- 已归档会话列表、搜索、恢复和永久删除。
- 现有批量删除会话改为批量归档。
- JSON schema 演进、Gateway API、领域服务、前端与自动化测试。

### 2.2 不包含

- 自动清理归档内容。
- 归档保留天数配置。
- 回收站容量限制。
- 跨会话移动任务。
- 恢复任务后自动重新运行。
- 永久删除未归档对象。
- 服务端全文检索或数据库索引。

## 3. 产品行为

### 3.1 定时任务普通列表

- 默认只展示 `archivedAt == null` 的任务。
- 搜索仅匹配会话名称和任务指令，忽略大小写并去除首尾空白。
- 状态筛选至少包含：全部、运行中、已完成、失败。
- 搜索和状态筛选组合使用。
- 点击列表项后在详情区域展示完整规则、受管上下文和 Occurrence 历史。
- 页面根容器使用 `height: 100dvh` 与纵向 `overflow-y: auto`，保证创建表单、列表和详情均可访问。
- 任务数量较多时，列表区域允许独立滚动；页面滚动仍作为整体兜底。

### 3.2 编辑定时任务

- “编辑”复用创建任务表单，并预填 Session、指令、计划、Workspace revision、受管权限和 Skill。
- 可编辑字段与创建字段相同；任务 ID 和既有 Occurrence 历史不变。
- 保存编辑前，服务端先阻止该 Task 形成新的 Occurrence。
- 服务端取消该 Task 尚未开始的 Runtime 请求，并停止其运行中或等待审批的 Run。
- 被编辑终止的 Occurrence 保留历史，并记录稳定的取消原因。
- 旧执行全部进入终态后，服务端保存新配置、重新计算 `nextRunAt`，并把任务恢复为 `active`。
- 如果终止或保存失败，任务不得以“部分旧配置、部分新配置”继续调度；API 返回稳定错误，前端保留编辑内容并允许重试。
- 编辑已取消但已恢复到普通列表的任务时，保存成功会重新激活该任务。

### 3.3 取消与已归档任务

- 普通列表不提供永久删除。
- “取消任务”停止未来触发，终止排队和运行中的该任务执行，然后设置 `archivedAt`。
- 取消后的任务自动从普通列表移入“已归档任务”。
- 已归档任务可查看任务配置和完整 Occurrence 历史。
- 已归档任务支持按会话名称和任务指令搜索。
- “恢复”只清除归档标记；任务状态仍为 `cancelled`，不会自动运行。
- 恢复后的已取消任务可通过“编辑并保存”重新激活。
- “永久删除”仅对已归档任务开放。二次确认后删除 Task JSON 和其全部 Occurrence JSON，不可恢复。

### 3.4 会话归档

- 普通会话列表和普通搜索只包含 `archivedAt == null` 的会话。
- 现有单个删除和批量删除操作改为单个归档与批量归档。
- 归档会话前，RuntimeHost 终止该会话所有排队、运行和等待审批的请求。
- 归档会话同时取消并归档所有关联且未归档的定时任务。
- 级联归档任务保留各自配置和 Occurrence 历史，并记录归档原因来自会话归档。
- 归档后的会话拒绝新消息、附件写入、Workspace 变更和新定时任务绑定。
- 如果运行终止或关联任务归档失败，会话不得被标记为归档；API 返回可重试错误。

### 3.5 已归档会话

- 侧栏提供“已归档会话”入口，与普通会话列表分离。
- 已归档会话支持按会话名称搜索。
- 归档详情允许只读查看原消息和基本信息。
- “恢复”清除会话归档标记，原消息、Run 历史、WorkspaceBinding、Skill 与统计信息完整保留。
- 恢复会话不自动恢复或激活关联定时任务；任务仍留在“已归档任务”。
- “永久删除”仅对已归档会话开放，并要求二次确认。
- 永久删除会话时，服务端同时永久删除该会话所有已归档任务及 Occurrence、附件元数据和附件内容、下载快照等会话从属数据。
- 永久删除失败时返回失败项；未确认完成的数据不得在 UI 中假装消失。

## 4. 数据模型与持久化

### 4.1 Session

Session 增加：

```text
archived_at: datetime | None
archived_reason: SessionArchiveReason | None
```

`SessionArchiveReason` 首期包含：

```text
user_archived
```

约束：

- `archived_at` 使用 UTC。
- `archived_at` 与 `archived_reason` 必须同时为空或同时有值。
- 写入 Session schema v7。
- reader 继续读取 v1–v6，并把缺失归档字段解释为未归档。

### 4.2 Task

Task 增加：

```text
archived_at: datetime | None
archived_reason: TaskArchiveReason | None
```

`TaskArchiveReason` 首期包含：

```text
user_cancelled
session_archived
```

约束：

- 已归档任务不能是 `active` 或 `cancel_requested`。
- 已归档任务的 `next_run_at` 必须为空。
- `archived_at` 与 `archived_reason` 必须同时为空或同时有值。
- 写入 Task schema v3。
- reader 继续读取 v1–v2，并把缺失归档字段解释为未归档。

### 4.3 Occurrence

Occurrence 保留，不因软归档删除。编辑或归档导致的终止应有可区分的稳定终态或错误码，例如：

```text
CANCELLED_BY_TASK_EDIT
CANCELLED_BY_TASK_ARCHIVE
CANCELLED_BY_SESSION_ARCHIVE
```

最终采用新增终态还是复用现有取消终态并保存 `errorCode`，由实施计划按现有状态机最小修改原则确定；对外必须可区分以上三个原因。

### 4.4 Store 接口

SessionStore 与 TaskStore 增加明确的归档查询和永久删除能力。普通 `list` 默认排除归档项；归档列表必须显式请求，避免调用方忘记过滤。

永久删除采用受限目标：

- SessionStore 只能删除一个已确认归档的 Session ID。
- TaskStore 只能删除一个已确认归档的 Task ID 及其 Occurrence 目录。
- 删除路径继续经过现有 ID 校验，不能接受路径、glob 或任意目录。

## 5. 服务与并发语义

### 5.1 Task 编辑

SchedulerService 在自己的异步锁内完成编辑协调：

1. 加载任务并拒绝已归档任务的直接编辑。
2. 暂停未来触发，清空 `nextRunAt`。
3. 取消 queued Occurrence。
4. 通过 RuntimeHost 停止 running 或 waiting-approval Run。
5. 对账并持久化相关 Occurrence 终态。
6. 原子保存新配置，重新计算 `nextRunAt`，状态设为 `active`。
7. 唤醒 Scheduler 重新计算最近 deadline。

同一个 Task 的并发编辑、取消和触发由同一锁串行。重复提交使用请求幂等键或等价的版本检查，避免双重激活。

### 5.2 会话归档

会话归档由一个应用层协调服务编排 RuntimeHost、SchedulerService、SessionStore、AttachmentStore 与下载快照服务。操作顺序：

1. 在 Session 协调锁内阻止新请求进入。
2. 终止 queued、running、waiting-approval 请求。
3. 取消并归档关联任务。
4. 保存 Session 归档字段。
5. 从普通会话查询中排除。

恢复只改变 Session 归档字段，不改变关联任务状态。

### 5.3 永久删除

- 永久删除前再次从 Store 加载对象并验证仍处于归档状态。
- 会话永久删除先清理从属数据，最后删除 Session 主文档。
- 中途失败返回稳定错误并保留足够事实供重试；重复请求应安全。
- UI 只有收到服务端成功响应后才移除条目。

## 6. Gateway API

具体路径沿用现有 `/api/v1` 风格，建议接口：

```text
PUT    /tasks/{task_id}
POST   /tasks/{task_id}/archive
POST   /tasks/{task_id}/restore
DELETE /tasks/{task_id}
GET    /tasks?archived=false|true

POST   /sessions/{session_id}/archive
POST   /sessions/{session_id}/restore
DELETE /sessions/{session_id}
GET    /sessions?archived=false|true
POST   /sessions/archive-batch
```

规则：

- 修改状态的请求继续要求本地 Cookie、同源校验和严格 JSON。
- `DELETE` 仅永久删除已归档对象。
- archived 参数默认 `false`。
- 任务搜索和状态筛选首期在前端完成，不增加服务端搜索语法。
- API 返回对象的 `archivedAt`、`archivedReason` 和最新状态，前端不自行推断。

## 7. 前端设计

### 7.1 SchedulerView

- 维持“左侧列表、右侧详情”结构。
- 顶部提供“新建任务”“已归档任务”“返回会话”。
- 列表上方增加搜索框和状态下拉框。
- 普通与归档视图使用同一列表组件和不同操作集。
- 创建与编辑共用表单状态；编辑模式显示“保存修改”和“取消编辑”。
- 归档视图只显示“恢复”“永久删除”。
- 永久删除使用确认对话框，明确任务及历史不可恢复。
- 当前 Gateway 返回的 Skill reference 是结构化对象；前端类型改为结构化 `SkillReference`，详情显示 `skillId` 与版本，不再把对象当字符串拼接。

### 7.2 App 会话侧栏

- 普通会话列表下方增加“已归档会话”入口。
- 现有批量管理中的“删除”改名为“归档”。
- 已归档会话视图提供名称搜索、选择、恢复和永久删除。
- 归档详情为只读；Composer、附件写入、Workspace 修改和定时任务创建入口禁用。
- 归档当前选中会话后，自动选择下一个普通会话；没有普通会话时显示空状态。
- 保留当前工作区选择器和设置入口的未提交改动，不回退或覆盖无关工作。

### 7.3 可访问性与响应式

- 搜索框、筛选器、列表和归档入口有可读 label。
- 对话框支持 Escape、焦点回收和明确的危险按钮。
- 桌面端列表独立滚动；窄屏退化为单列，详情位于列表之后。
- 归档、恢复、永久删除的忙碌状态禁用重复点击。
- 页面滚动条使用浏览器原生行为，不隐藏滚动条。

## 8. 错误处理

至少提供以下稳定错误：

```text
TASK_ARCHIVED
TASK_EDIT_INTERRUPTION_FAILED
TASK_NOT_ARCHIVED
SESSION_ARCHIVED
SESSION_ARCHIVE_INTERRUPTION_FAILED
SESSION_ARCHIVE_TASK_FAILED
SESSION_NOT_ARCHIVED
PERMANENT_DELETE_FAILED
```

- 422：字段或计划规则无效。
- 404：对象不存在。
- 409：归档状态冲突、对象正在转换、终止执行失败。
- 500：持久化或清理失败，响应不泄露本机绝对路径。

前端保留用户输入，显示后端安全错误信息，并允许重试。

## 9. 测试与验收

### 9.1 模型与 Store

- Session v1–v6 读取为未归档，v7 往返一致。
- Task v1–v2 读取为未归档，v3 往返一致。
- 归档字段组合、UTC 和状态约束。
- 普通列表排除归档项，归档列表仅返回归档项。
- 永久删除归档 Task 时删除全部 Occurrence。
- 未归档对象永久删除被拒绝。

### 9.2 Runtime 与 Scheduler

- 编辑前取消 queued、停止 running 和 waiting-approval。
- 编辑期间不产生新 Occurrence。
- 编辑成功后保留历史、任务 ID 不变、按新规则计算下一次执行。
- 取消任务后状态为 cancelled、无 next run、自动归档。
- 恢复任务不自动运行；编辑保存后重新激活。
- Gateway 重启后不会恢复归档任务。

### 9.3 会话

- 归档终止 queued、running、waiting-approval。
- 归档会话级联归档所有关联任务。
- 归档会话拒绝新请求。
- 恢复会话保留消息和元数据，但不恢复任务。
- 批量归档返回逐项结果。
- 永久删除会话清理关联任务、Occurrence、附件与从属数据。

### 9.4 Gateway 与前端

- API 认证、同源校验、状态冲突和幂等行为。
- 搜索只匹配会话名称和任务指令。
- 状态筛选与搜索可组合。
- 普通列表看不到归档对象。
- 归档视图可恢复和永久删除。
- 定时任务页面在桌面和窄屏均可滚动。
- 编辑表单预填和失败保留输入。
- 永久删除必须二次确认。

### 9.5 验证命令

```powershell
python -m pytest -q
python -m mypy src
python -m ruff check .
python -m compileall -q src
cd web
npm run build
```

如果项目已有前端测试基础设施，再增加 SchedulerView 与归档会话交互测试；否则至少通过 TypeScript 构建和 Gateway 集成测试覆盖数据流。

## 10. 风险与控制

- **跨多个 JSON 文件的级联操作不是数据库事务。** 使用稳定归档字段、幂等步骤和可重试清理顺序，避免 UI 假装完成。
- **编辑与运行终止存在竞态。** Scheduler 锁负责阻止新触发，RuntimeHost 负责在 Session 锁内取消或停止。
- **永久删除不可恢复。** 只允许归档对象、二次确认、服务端再次校验。
- **会话与任务存在级联关系。** 归档会话级联归档任务；恢复会话不自动恢复任务，避免意外重新执行。
- **当前前端存在未提交改动。** 实施时仅在需要的区域做小范围合并，不能覆盖用户已有工作区选择器和设置入口修改。

## 11. 验收标准

满足以下条件才算完成：

1. 定时任务页面可正常纵向滚动。
2. 普通任务列表支持搜索、状态筛选、选择、查看和编辑。
3. 编辑任务会终止旧的排队、运行和等待审批执行，历史保留。
4. 取消任务自动进入已归档任务，普通搜索不可见。
5. 已归档任务可恢复和永久删除。
6. 删除会话改为归档，并终止活动请求、级联归档任务。
7. 已归档会话可恢复和永久删除，普通列表不可见。
8. 永久删除只能发生在归档区，并要求确认。
9. 归档与恢复在 Gateway 重启后仍保持。
10. 相关测试、静态检查和前端构建通过，用户现有未提交改动得到保留。
