# SJTUClaw 自动长期记忆与记忆时间线设计

日期：2026-07-28
状态：已确认，等待书面审阅

## 1. 背景

SJTUClaw 已经具有手动长期记忆：

- `MemoryEntry` 表示一条跨 Session 记忆；
- `JsonMemoryStore` 将记忆保存到 `memory.json`；
- CLI 支持添加、查看和删除；
- `ContextBuilder` 会把记忆加入模型上下文。

现有实现只支持手动管理，并在每次请求中注入全部记忆。它缺少自动提取、更新去重、相关检索、时间归一化、可编辑规则以及 Gateway/UI 管理入口。如果直接在每轮对话后追加文本，记忆会持续膨胀，最终占满稳定上下文。

本设计在保留现有模块化单体架构的前提下，加入自动长期记忆和轻量检索，不引入向量数据库、图数据库或第三方记忆平台。

## 2. 目标

1. 每个成功的直接用户对话轮次结束后，额外调用一次当前 LLM，自动判断应该新增、更新或忽略哪些长期记忆。
2. 默认使用一套智能记忆规则：
   - 用户长期偏好、身份与背景、长期项目、明确要求和约束属于必须考虑的内容；
   - 其他预计会在未来 Session 中复用的信息由模型判断；
   - 普通寒暄、一次性请求和未经确认的推测不应写入。
3. 用户可以在 UI 中编辑自然语言记忆规则，并恢复默认规则。
4. 记忆自动写入保持安静，不要求用户逐轮审核，也不显示保存 Toast。
5. 用户可以通过顶部“记忆”按钮打开右侧抽屉，搜索、查看、编辑和删除记忆。
6. 使用纵向时间线展示记忆；每条记忆是一个节点。
7. 可解析的相对时间转换为绝对时间；无法可靠解析时保留原表达，不猜测。
8. 每次模型调用只注入 Top-K 相关记忆，并受独立 Token 上限控制。
9. 保持现有会话搜索、聊天、Session、Scheduler、活动栏、附件、审批、Workspace 和 Skill 行为。
10. 为以后替换为向量或混合 RAG 预留 `MemoryRetriever` 接口。

## 3. 非目标

- 不接入 Mem0、Zep、Graphiti、LangGraph Memory 或其他外部记忆平台。
- 不引入 Qdrant、Chroma、Neo4j、FalkorDB 或新的 Embedding 服务。
- 不实现知识图谱、关系遍历或双时间图模型。
- 不从 Scheduler 执行结果自动写长期记忆。
- 不让模型自动删除记忆。
- 不提供每轮记忆通知、逐条审批、即时撤销 Toast 或记忆回收站。
- 不重写现有 Session、Compaction、Tool、Scheduler 或 Skill 架构。
- 不顺带修复与本功能无关的既有 mypy 测试类型错误。

## 4. 核心决策

### 4.1 单一智能模式

系统不提供“保守/智能”模式开关。自动记忆始终启用，并使用一个完整规则集合：

- 长期偏好、身份信息、长期项目背景、明确要求和约束必须进入提取判断；
- 其他未来可能跨 Session 有用的内容由模型判断；
- 用户可以修改规则文本，但不能关闭硬安全校验。

### 4.2 静默写入

自动提取和写入不显示逐轮通知。用户需要时打开记忆抽屉统一管理。

记忆条目仍保留：

- 自动或手动来源；
- 来源 Session 和 Message；
- 记录时间；
- 可用时的事件时间与原始相对时间表达。

持续性 Store 故障可以在“记忆”按钮上显示非打扰式异常状态，但普通提取失败不通知用户。

### 4.3 轻量检索

第一版使用本地确定性排序，不使用向量：

- Unicode NFKC 归一化和大小写折叠；
- 拉丁字母与数字分词；
- CJK 字符二元组匹配；
- 完整子串匹配加权；
- 记忆类别、重要度和时间衰减加权；
- 去重后选择 Top-K；
- 超过 Memory Context Token 上限时按得分截断。

`MemoryRetriever` 是独立 Port。未来接向量或混合 RAG 时，只替换 Retriever 实现，不修改 Extractor、Memory Service、Gateway API 或 UI。

## 5. 架构

### 5.1 新增职责

#### `MemoryService`

长期记忆唯一业务入口，负责：

- 手动 CRUD；
- 自动候选应用；
- Revision 冲突检查；
- 时间字段校验；
- 敏感内容过滤；
- 全局 Memory Lock；
- 调用 Store 持久化。

现有 `SessionService` 不再承担新增的 Memory 业务规则。CLI 的 `/memory` 命令改为调用 `MemoryService`。这是与功能直接相关的职责移动，不扩展到 Session 以外的无关重构。

#### `MemoryExtractor`

负责在一次正常回答完成后：

1. 读取当前有效记忆规则；
2. 检索少量相关已有记忆，作为去重和更新上下文；
3. 将当前 Clock 时间、本机 IANA 时区、当前用户输入、当前 Turn 的 Tool Result
   和最终回答作为结构化数据交给 LLM；
4. 解析严格 JSON；
5. 返回 `ADD`、`UPDATE` 或 `NOOP` 候选。

Extractor 不直接写文件，也不能执行 `DELETE`。

#### `MemoryPolicyService`

负责：

- 读取包内默认规则；
- 读取、保存和重置用户 override；
- 校验规则非空、长度和控制字符；
- 向 Extractor 提供当前有效规则。

用户规则只影响“应该记什么”，不改变主 System Prompt、Soul、Tool 权限或硬安全边界。

#### `MemoryRetriever`

负责从 Store 中检索与当前用户问题相关的记忆，并返回有序结果。`ContextBuilder` 只消费 Retriever 结果，不再直接加载并注入全部记忆。

#### `MemoryCoordinator`

提供跨 Session 的全局异步锁。SJTUClaw 允许不同 Session 并行执行，而 `memory.json` 是全局共享文件；所有 read-modify-write 操作必须在同一个 Memory Lock 下完成，避免丢失更新。

### 5.2 写入数据流

```text
直接用户请求（CLI / Gateway）
-> AgentLoop 完成正常模型与 Tool 循环
-> 保存最终 Assistant Message
-> 正常回答可被用户看到
-> MemoryExtractor 额外调用一次当前 LLM
-> 解析 ADD / UPDATE / NOOP
-> MemoryService 执行安全过滤、去重、时间校验和 Revision 校验
-> 在 Memory Lock 下原子保存
-> Run 正常结束
```

提取失败是非关键副作用失败：

- 不删除或回滚已经保存的 Assistant Message；
- 不把正常 Run 改为失败；
- 不向用户显示逐轮错误；
- 只记录不含敏感内容的内部状态。

### 5.3 读取数据流

```text
当前 Session 的最新用户问题
-> MemoryRetriever
-> 关键词、类别、重要度与时间排序
-> Top-K + Token Budget 截断
-> ContextBuilder 生成 stable_memory Message
-> AgentLoop 调用 LLM
```

Compaction 仍只处理 Session Summary 和 Session Messages，不改写长期记忆。Compaction 预算计算使用本轮 Retriever 选择的记忆，而不是整个 Memory Store。

### 5.4 自动提取来源

自动提取只在以下来源运行：

- CLI 普通用户消息；
- Gateway 普通用户消息。

以下来源不自动提取：

- Scheduler；
- Session/Memory/Skill 等本地命令；
- Compaction；
- 自动标题生成；
- Provider 设置操作。

Extractor 可以参考当前 Turn 的 Assistant Answer 和 Tool Result，但只允许保存：

- 用户明确表达的事实、偏好、要求或决定；
- Tool Result 能直接支持的项目事实；
- 用户确认过的 Assistant 建议。

Assistant 自己生成、用户未确认、Tool Result 也不能支持的事实不得写入。

## 6. 默认记忆规则

默认规则从独立包内 UTF-8 文件加载，建议文件名：

```text
src/sjtuclaw/prompts/memory_rules.md
```

默认内容：

```text
请自动记住对未来对话有持续帮助的信息，包括用户稳定偏好、身份与背景、
长期项目、明确要求和约束，以及其他可能跨会话复用的信息。

忽略寒暄、一次性请求、未经用户确认的推测，以及 Assistant 自己生成但
用户没有确认、Tool Result 也不能支持的事实。

把可以确定的相对日期转换为绝对日期；无法确定时保留原表达，不要猜测。
```

用户 override 单独持久化。规则编辑：

- 使用自然语言文本框；
- 保存后只影响后续提取；
- 不回溯重处理已有记忆；
- 支持恢复默认；
- 空文本、超长文本和非法控制字符拒绝保存；
- 旧的有效规则在保存失败时继续生效。

## 7. 数据模型

### 7.1 `MemoryEntry v2`

```text
memoryId: string
content: string
category: identity | preference | project | constraint | event | other
importance: integer, 1..5
sourceKind: automatic | manual
sourceSessionId: string | null
sourceMessageId: string | null
originalTimeText: string | null
eventAt: ISO-8601 timestamp | null
eventTimezone: IANA timezone | null
createdAt: ISO-8601 timestamp
updatedAt: ISO-8601 timestamp
revision: positive integer
```

约束：

- `content` 去除首尾空白后不能为空；
- 自动记忆必须具有 `sourceSessionId` 和 `sourceMessageId`；
- `eventAt` 为空时 `eventTimezone` 也为空；
- `revision` 每次编辑或自动更新后加一；
- `updatedAt` 不早于 `createdAt`；
- 相对时间无法可靠解析时，保留 `originalTimeText`，`eventAt` 为空；
- 用户编辑记忆时可以修改内容、类别和事件时间；
- `memoryId`、来源和创建时间不可由 UI 修改。

### 7.2 Memory Document v2

```json
{
  "schemaVersion": 2,
  "entries": []
}
```

Loader 严格支持 v1 和 v2。v1 Entry 升级默认值：

```text
category = other
importance = 3
sourceKind = manual
sourceSessionId = null
sourceMessageId = null
originalTimeText = null
eventAt = null
eventTimezone = null
updatedAt = createdAt
revision = 1
```

首次正常保存时原子升级为 v2。不做启动时批量迁移，不覆盖损坏文件。

### 7.3 Memory Policy Document

```text
schemaVersion: 1
rules: string
updatedAt: ISO-8601 timestamp
revision: positive integer
```

不存在 override 文件时使用包内默认规则。恢复默认会删除 override，而不是复制一份默认文本。

## 8. 提取协议

Extractor 使用严格 JSON，顶层结构：

```json
{
  "operations": [
    {
      "type": "ADD",
      "content": "用户偏好使用中文回答。",
      "category": "preference",
      "importance": 5,
      "originalTimeText": null,
      "eventAt": null,
      "eventTimezone": null
    }
  ]
}
```

允许操作：

```text
ADD
UPDATE
NOOP
```

`UPDATE` 必须引用 Extractor 输入中提供的现有 `memoryId` 和 `revision`。整个响应采用全批次校验：任一操作格式无效时，本次不应用任何候选，避免部分写入形成不一致状态。

MemoryService 再执行确定性校验。LLM 的输出不具有直接写权限。

## 9. 时间归一化

SJTUClaw 是本地单用户应用。时间解析使用本机 IANA 时区；保存时同时记录 UTC 时间和原时区。

规则：

- “明天”“下周三”等可确定表达，基于当前 Clock 和本机时区解析；
- “过段时间”“以后找机会”等模糊表达不生成 `eventAt`；
- 原始表达保存在 `originalTimeText`；
- 修改记忆的事件时间时，同步更新 `eventAt`、`eventTimezone` 和 `revision`；
- 时间线显示时转换为浏览器本地时区；
- UI 同时提供相对显示和精确时间。

时间线排序键：

```text
eventAt（存在时）
否则 createdAt
```

节点必须明确标注“事件时间”或“记录时间”，不能把两者混为一谈。未来事件可以显示在“今天”之前。

## 10. 检索与上下文预算

默认配置：

```text
MEMORY_RETRIEVAL_LIMIT = 8
MEMORY_CONTEXT_MAX_TOKENS = 2048
```

Retriever 的确定性排序因素：

1. 当前问题中的完整子串命中；
2. 拉丁 Token 或 CJK 二元组重合度；
3. `identity`、`preference`、`constraint` 等稳定类别加权；
4. `importance`；
5. 时间衰减；
6. `updatedAt` 作为稳定 tie-breaker；
7. `memoryId` 作为最终确定性 tie-breaker。

Retriever 先按得分选取候选，再按 Token Budget 截断。任何情况下都不得因为 Memory 超长挤掉 System Prompt、Soul 或模型输出预留空间。

没有查询文本时，例如 Compaction 计算 stable context，Retriever 只返回最高优先级的少量稳定记忆。

## 11. Gateway API

所有接口沿用现有本地同源认证、错误映射和 API Prefix。

### 11.1 Memory

```text
GET /memories?query=<text>&cursor=<opaque>&limit=<n>
POST /memories
PATCH /memories/{memoryId}
DELETE /memories/{memoryId}?revision=<n>
```

行为：

- `GET` 返回按时间线排序的分页结果；
- `POST` 创建 `sourceKind=manual` 的记忆；
- `PATCH` 必须携带当前 `revision`；
- Revision 不匹配返回 `409`；
- 参数无效返回 `422`；
- Store 不可用返回 `503`；
- `DELETE` 永久删除，不提供回收站。

### 11.2 Memory Policy

```text
GET /memory-policy
PUT /memory-policy
POST /memory-policy/reset
```

`GET` 返回：

- 当前有效规则；
- 是否为用户 override；
- 当前 revision；
- 默认规则内容，供 UI 恢复前预览。

### 11.3 无逐轮 Memory Event

Gateway 不为每次自动写入新增用户可见 SSE 事件。现有 Run Event 流程保持不变。

## 12. UI

### 12.1 顶部工具栏

桌面布局从三列扩展为：

```text
左侧 Session 栏 | 中间聊天栏 | 右侧活动栏
                 \_____ 共享顶部工具栏 _____/
```

工具栏横跨中间栏和右栏，按钮靠右排列：

```text
记忆
定时任务
已归档会话
设置
```

左侧栏继续保留：

- 新建 Session；
- 普通会话和已归档会话切换；
- 普通会话标题搜索；
- 已归档会话标题搜索；
- Session 列表和批量管理。

### 12.2 记忆抽屉

点击“记忆”后从右侧滑出抽屉：

- 聊天保持可见；
- 抽屉覆盖右侧活动栏和部分中间栏；
- 桌面宽度约 390px；
- 移动端接近全宽；
- 关闭后恢复原活动栏。

抽屉包含两个标签：

```text
时间线
记忆规则
```

### 12.3 时间线

- 纵向连接线；
- 每条记忆是一个圆点节点；
- 日期或月份形成分组节点；
- 轻微颜色区分偏好/身份、事件、项目等类别；
- 节点显示内容、时间类型、相对时间、精确时间、来源和自动/手动标识；
- 节点支持编辑和删除；
- 搜索只过滤 Memory 节点，不影响现有 Session 搜索；
- 过滤后连接线只串联可见节点。

编辑在抽屉内展开，允许修改：

- 内容；
- 类别；
- 事件时间。

删除需要一次确认，并永久删除。

### 12.4 记忆规则

- 自然语言多行编辑框；
- 保存；
- 恢复默认；
- 显示当前是否为 override；
- 保存成功后刷新 revision；
- 保存失败时保留用户未提交文本，并继续使用旧规则。

### 12.5 安静运行

UI 不显示：

- “已记住 N 条”Toast；
- 每轮审核面板；
- 自动记忆通知；
- 即时撤销入口。

## 13. 安全边界

以下规则在代码中执行，用户规则不能覆盖：

- 拒绝保存 API Key、密码、验证码、Authorization Header、支付凭据等秘密；
- 自动 Extractor 不能删除记忆；
- 自动记忆必须具有直接来源；
- Assistant 未经用户确认且无 Tool Result 支持的推测不得保存；
- 用户规则只进入 Extractor，不进入主 Agent System Prompt；
- 用户规则不能授予 Tool 权限；
- 候选内容和规则文本均有长度上限；
- 日志不得记录被过滤的秘密正文；
- Store 损坏时不得静默覆盖原文件。

用户主动通过手动 Memory API 输入内容时仍执行秘密过滤。该边界优先于“用户可以编辑规则”。

## 14. 错误处理

| 场景 | 行为 |
| --- | --- |
| Extractor 网络或 LLM 失败 | 不写记忆；正常回答保持成功；记录安全内部状态 |
| Extractor 返回空内容或非法 JSON | 整批丢弃；不部分写入 |
| 候选含秘密 | 丢弃对应候选；日志不记录原文 |
| 时间表达不确定 | 保留原表达；`eventAt=null` |
| Revision 冲突 | 自动更新重新读取后最多重试一次；UI 编辑返回 `409` 并刷新 |
| Memory Store 写入失败 | 不替换旧文件；Memory 按钮显示持续异常状态 |
| Memory Store 损坏 | 不覆盖；Memory API 返回明确错误；Agent Turn 不使用损坏记忆 |
| 规则为空或非法 | 返回 `422`；继续使用旧规则 |
| 默认规则文件为空或缺失 | 启动配置错误，不使用空规则运行 |

Memory Store 损坏时，普通聊天可以在不注入 Memory 的情况下继续；这是显式降级。UI 的 Memory 按钮显示异常状态，避免静默假装记忆仍然可用。

## 15. 测试与验收

### 15.1 单元测试

- Memory v2 字段与约束；
- Extractor 严格协议和全批次拒绝；
- `ADD`、`UPDATE`、`NOOP`；
- 自动删除拒绝；
- A 类内容提取合同；
- C 类扩展内容；
- 秘密过滤；
- 相对日期归一化和模糊日期保护；
- CJK 二元组与拉丁 Token 检索；
- 类别、重要度、时间衰减和稳定 tie-breaker；
- Top-K 与 Token Budget；
- 默认规则、override、保存和恢复；
- Revision 冲突；
- 静默失败不影响正常回答。

### 15.2 Store 契约测试

- v1/v2 严格读取；
- v1 首次保存原子升级；
- 创建、读取、更新和删除；
- 损坏文件不覆盖；
- 重复 ID 拒绝；
- 全局并发写不丢更新；
- Policy Store 的默认、override 和 reset。

### 15.3 Runtime 与 Gateway 集成测试

- CLI/Gateway 直接用户 Turn 触发一次额外提取；
- Scheduler 不自动写记忆；
- 正常回答先持久化；
- Extractor 失败后 Run 仍成功；
- ContextBuilder 只注入 Retriever 结果；
- Compaction 不改写 Memory；
- Memory CRUD 和 Policy API；
- 本地认证继续保护新增接口；
- Revision 冲突和 Store 错误映射；
- 不产生用户可见 Memory SSE 事件。

### 15.4 前端验收

- 顶部工具栏横跨中间栏和右栏；
- 记忆抽屉可打开、关闭和响应式显示；
- 时间线节点、连接线、分组和类别颜色；
- 搜索、编辑、删除；
- 事件时间与记录时间标记；
- 相对时间和精确时间；
- 规则编辑、保存和恢复默认；
- Memory 故障状态；
- 现有普通/已归档 Session 搜索不丢失；
- Scheduler、归档入口、设置和活动栏仍可使用；
- 无逐轮记忆通知。

### 15.5 验证命令

```powershell
py -3.12 -m pytest -m "not live"
py -3.12 -m ruff check . --no-cache
py -3.12 -m mypy src --no-incremental
Set-Location web
npm run build
```

完整 `mypy src tests` 当前存在 13 个与本功能无关的基线测试类型错误。实施期间必须保证：

- `mypy src` 通过；
- 新增或修改的测试文件没有新增类型错误；
- 完整检查不比基线更差；
- 不为追求全绿进行无关重构。

## 16. 风险与取舍

### 16.1 静默误记忆

没有逐轮审核会增加错误记忆长期存在的风险。缓解方式：

- 严格来源；
- 不保存未经确认的 Assistant 推测；
- 相关已有记忆参与更新判断；
- UI 提供来源、搜索、编辑和删除；
- 用户可以修改规则。

### 16.2 轻量检索召回不足

关键词和 CJK 二元组不能完全替代语义 Embedding。当前取舍是保持本地、低依赖和可验收。达到数百条记忆或出现明确召回问题后，再通过 `MemoryRetriever` Port 接入向量或混合检索。

### 16.3 每轮额外成本

每个成功直接用户 Turn 最多增加一次 LLM 调用。用户已接受该成本。Extractor 使用紧凑输入、少量相关已有记忆和严格短 JSON 输出控制 Token。

### 16.4 时间推断错误

相对日期依赖当前时间和时区。无法可靠确定时不生成绝对时间；UI 始终保留原表达并区分事件时间与记录时间。

## 17. 完成定义

满足以下条件才算功能完成：

1. 自动提取、轻量检索、规则管理、Gateway API 和 UI 时间线全部接入同一 Runtime；
2. 现有手动 Memory 数据可安全迁移；
3. 自动提取失败不影响正常聊天；
4. 记忆不会再全量注入模型；
5. 顶部工具栏和右侧记忆抽屉符合已确认布局；
6. 现有 Session 搜索和其他工作台功能保持；
7. 相关测试、静态检查和前端构建达到本设计的验证标准；
8. 文档说明变更、验证方式、未覆盖范围和风险。
