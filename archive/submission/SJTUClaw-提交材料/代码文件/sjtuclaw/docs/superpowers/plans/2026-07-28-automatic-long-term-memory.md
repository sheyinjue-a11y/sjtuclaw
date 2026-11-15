# Automatic Long-Term Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 SJTUClaw 增加默认静默运行的自动长期记忆、可编辑记忆规则、轻量本地检索，以及位于中栏和右栏上方的工具栏与时间线记忆抽屉。

**Architecture:** `MemoryService` 独占记忆业务规则，`MemoryPolicyService` 管理默认规则和用户 override，`MemoryExtractor` 在成功的 CLI/Gateway 用户 Turn 主回答落盘并发出后最多调用一次 LLM，`LocalMemoryRetriever` 用关键词、CJK 二元组、类别、重要性和时间进行本地排序。JSON Store 严格读取 v1/v2、只在正常写入时原子升级。React 只通过 Gateway 管理记忆，不接收逐轮记忆通知。

**Tech Stack:** Python 3.12、FastAPI/Pydantic、严格原子 JSON Store、`tzlocal`/`zoneinfo`、React 19、TypeScript、Vite、pytest、ruff、mypy。

## Global Constraints

- 单一智能记忆模式；A 类核心信息包含在 C 类广义记忆规则中。
- 成功的直接 CLI/Gateway Turn 最多增加一次 LLM；主回答先展示，记忆失败不影响 Run。Scheduler 不写记忆。
- 自动操作限 `ADD/UPDATE/NOOP`；秘密过滤、来源和 revision 由代码强制。
- 当前采用本地轻量检索，并保留未来替换 Retriever 的接口。
- 工具栏横跨中栏和右栏；记忆抽屉右滑；现有普通/归档会话搜索保持独立可用。
- 非 live 基线 292 tests；`mypy src tests` 的既有 13 errors 不得增加，`mypy src` 必须通过。

## Public Interfaces and Data Contracts

```python
class MemoryCategory(StrEnum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    PROJECT = "project"
    CONSTRAINT = "constraint"
    EVENT = "event"
    OTHER = "other"

class MemorySourceKind(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"

class MemoryStore(Protocol):
    def load(self) -> Memory: ...
    def replace(self, memory: Memory) -> Memory: ...

class MemoryRetriever(Protocol):
    def retrieve(self, query: str) -> tuple[MemoryEntry, ...]: ...
```

Memory API：

```text
GET    /api/v1/memories?query=<text>&cursor=<opaque>&limit=<1..100>
POST   /api/v1/memories
PATCH  /api/v1/memories/{memoryId}
DELETE /api/v1/memories/{memoryId}?revision=<positive-int>

GET    /api/v1/memory-policy
PUT    /api/v1/memory-policy
POST   /api/v1/memory-policy/reset
```

分页响应固定为：

```json
{
  "items": [],
  "nextCursor": null
}
```

时间线按 `eventAt ?? createdAt` 倒序，再按 `memoryId` 排序。Cursor 是 Gateway 生成和解析的 URL-safe opaque token；前端不读取内部结构。

## File Structure

| File | Responsibility |
| --- | --- |
| `src/sjtuclaw/models/memory.py` | Memory v2、分类、来源、提取操作和约束。 |
| `src/sjtuclaw/ports/stores.py`, `ports/memory.py` | Memory/Policy Store 与 Retriever 边界。 |
| `src/sjtuclaw/infrastructure/persistence/json_store.py` | Memory v1/v2 严格读取、v2 原子写入。 |
| `src/sjtuclaw/infrastructure/persistence/memory_policy.py` | 用户记忆规则 override 的严格 JSON 持久化。 |
| `src/sjtuclaw/runtime/memory_coordinator.py` | 单进程全局 Memory 读改写锁。 |
| `src/sjtuclaw/runtime/memory_service.py` | CRUD、分页、revision、整批自动操作。 |
| `src/sjtuclaw/runtime/memory_policy.py` | 默认规则加载、override、reset。 |
| `src/sjtuclaw/runtime/memory_retriever.py` | 本地关键词/CJK/类别/重要性/时间检索和 Token 截断。 |
| `src/sjtuclaw/runtime/memory_extractor.py` | 一次 LLM 提取、严格 JSON 解析、来源约束。 |
| `src/sjtuclaw/runtime/memory_time.py` | 相对时间的确定性归一化。 |
| `src/sjtuclaw/runtime/memory_safety.py` | 不可覆盖的秘密过滤。 |
| `src/sjtuclaw/runtime/memory_capture.py` | 成功 Turn 的静默副作用编排。 |
| `src/sjtuclaw/prompts/memory_rules.md` | 包内默认自然语言规则。 |
| `src/sjtuclaw/prompts/memory_extraction_prompt.md` | Extractor 系统提示和严格协议。 |
| `src/sjtuclaw/bootstrap.py`, `entrypoints/{cli,gateway}.py` | 依赖组装、CLI 兼容、Gateway API。 |
| `web/src/MemoryDrawer.tsx` | 时间线、编辑、删除、规则页签。 |
| `web/src/memory.ts` | Memory API 类型、时间和分类显示工具。 |
| `web/src/App.tsx`, `web/src/styles.css` | 顶部工具栏、抽屉状态、响应式布局、故障标记。 |

## Task 1: Memory v2 Domain, Atomic Store, and Core Service

**Files:**

- Modify: `src/sjtuclaw/models/memory.py`
- Modify: `src/sjtuclaw/models/__init__.py`
- Modify: `src/sjtuclaw/ports/stores.py`
- Modify: `src/sjtuclaw/infrastructure/persistence/json_store.py`
- Create: `src/sjtuclaw/runtime/memory_coordinator.py`
- Create: `src/sjtuclaw/runtime/memory_service.py`
- Create: `src/sjtuclaw/runtime/memory_safety.py`
- Modify: `src/sjtuclaw/runtime/__init__.py`
- Modify: `tests/contract/memory_store_contract.py`
- Modify: `tests/contract/test_json_store_contract.py`
- Modify: `tests/unit/test_json_store.py`
- Create: `tests/unit/test_memory_models.py`
- Create: `tests/unit/test_memory_service.py`

**Interfaces produced:**

```python
MemoryEntry(
    memory_id, content, category, importance, source_kind,
    source_session_id, source_message_id, original_time_text,
    event_at, event_timezone, created_at, updated_at, revision
)

class MemoryService:
    def list_page(self, *, query: str, cursor: str | None, limit: int) -> MemoryPage: ...
    def create_manual(self, command: CreateMemory) -> MemoryEntry: ...
    def update_manual(self, memory_id: str, command: UpdateMemory) -> MemoryEntry: ...
    def delete_manual(self, memory_id: str, *, revision: int | None) -> None: ...
```

- [x] **Step 1: 写 Memory v2 和安全过滤的失败测试**

  覆盖分类枚举、importance `1..5`、UTC 时间、`updated_at >= created_at`、自动来源必须带 Session/Message ID、`event_at` 与 `event_timezone` 成对、空内容和秘密内容拒绝。

  ```python
  def test_automatic_memory_requires_provenance() -> None:
      with pytest.raises(ValueError, match="provenance"):
          MemoryEntry(
              memory_id="memory_1",
              content="用户偏好中文回答",
              category=MemoryCategory.PREFERENCE,
              importance=5,
              source_kind=MemorySourceKind.AUTOMATIC,
              source_session_id=None,
              source_message_id=None,
              created_at=NOW,
              updated_at=NOW,
          )
  ```

- [x] **Step 2: 运行 RED**

  Run: `py -3.12 -m pytest tests/unit/test_memory_models.py tests/contract/test_json_store_contract.py tests/unit/test_json_store.py -q`

  Expected: 新枚举、字段和 v2 Store 行为尚不存在。

- [x] **Step 3: 实现模型、秘密过滤和 v1/v2 Store**

  严格读取：

  - v1 Entry 映射为 `other / 3 / manual / revision=1`；
  - v2 只接受设计稿中的完整字段，不忽略未知字段；
  - `load()` 读取 v1 时只在内存中升级；
  - `replace()` 永远写 schema v2，并复用现有 `_atomic_write`；
  - 解析失败抛 `StoreError`，禁止覆盖原文件；
  - duplicate ID 拒绝。

  `MemoryCoordinator` 封装进程内 `threading.RLock`；同一个实例注入 Service、Capture 和 Gateway，串行化所有读改写。搜索匹配 `content/category/original_time_text`，分页排序稳定。秘密过滤先返回通用验证错误，不把命中的原文放进异常。

- [x] **Step 4: 写并运行 CRUD/revision/迁移测试**

  ```python
  def test_update_rejects_stale_revision_without_changing_entry() -> None:
      current = service.create_manual(command)
      service.update_manual(current.memory_id, replace_command(revision=current.revision))
      with pytest.raises(MemoryRevisionConflict):
          service.update_manual(current.memory_id, replace_command(revision=current.revision))
  ```

  Run: `py -3.12 -m pytest tests/unit/test_memory_models.py tests/unit/test_memory_service.py tests/unit/test_json_store.py tests/contract/test_json_store_contract.py -q`

  Expected: PASS；v1 文件第一次成功 CRUD 后变成 v2；损坏文件字节保持不变。

- [x] **Step 5: 静态检查并提交**

  Run: `py -3.12 -m ruff check src/sjtuclaw/models/memory.py src/sjtuclaw/runtime/memory_service.py src/sjtuclaw/runtime/memory_safety.py src/sjtuclaw/infrastructure/persistence/json_store.py`

  Commit: `feat: add versioned long-term memory store`

## Task 2: Editable Memory Policy

**Files:**

- Create: `src/sjtuclaw/prompts/memory_rules.md`
- Create: `src/sjtuclaw/ports/memory.py`
- Create: `src/sjtuclaw/infrastructure/persistence/memory_policy.py`
- Modify: `src/sjtuclaw/infrastructure/persistence/__init__.py`
- Create: `src/sjtuclaw/runtime/memory_policy.py`
- Modify: `src/sjtuclaw/runtime/__init__.py`
- Create: `tests/unit/test_memory_policy.py`

**Interfaces produced:**

```python
class MemoryPolicyStore(Protocol):
    def load(self) -> MemoryPolicy | None: ...
    def save(self, policy: MemoryPolicy) -> MemoryPolicy: ...
    def reset(self) -> None: ...

class MemoryPolicyService:
    def get(self) -> MemoryPolicySnapshot: ...
    def update(self, rules: str, *, revision: int) -> MemoryPolicySnapshot: ...
    def reset(self, *, revision: int) -> MemoryPolicySnapshot: ...
```

- [x] **Step 1: 写默认规则、override、revision 和 reset 的失败测试**

  默认规则必须明确包括 A 类核心内容、C 类扩展内容、忽略短期请求、禁止未确认推测、相对时间可解析才归一化。空白、超长、控制字符和 stale revision 均拒绝。

- [x] **Step 2: 运行 RED**

  Run: `py -3.12 -m pytest tests/unit/test_memory_policy.py -q`

  Expected: Policy Store/Service 和包内规则不存在。

- [x] **Step 3: 实现最小 Policy 链路**

  - 默认规则用 `importlib.resources.files("sjtuclaw.prompts")` 加载；
  - 缺失或空默认文件抛 `ConfigurationError`；
  - override 保存为 `memory_policy.json` schema v1，使用现有原子写辅助；
  - 无 override 时 `is_override=False`，revision 使用稳定的 `1`；
  - 首次 override revision 为 `2`，后续递增；
  - reset 校验 revision 后只删除精确 override 文件，返回默认快照；
  - 写入失败继续使用旧规则。

- [x] **Step 4: 运行 focused tests**

  Run: `py -3.12 -m pytest tests/unit/test_memory_policy.py -q`

  Expected: PASS，包括坏文件不覆盖、reset 后读取包内默认规则。

- [x] **Step 5: 提交**

  Commit: `feat: persist editable memory rules`

## Task 3: Lightweight Retrieval and Bounded Context Injection

**Files:**

- Modify: `src/sjtuclaw/ports/memory.py`
- Create: `src/sjtuclaw/runtime/memory_retriever.py`
- Modify: `src/sjtuclaw/runtime/context_builder.py`
- Modify: `src/sjtuclaw/config.py`
- Modify: `.env.example`
- Create: `tests/unit/test_memory_retriever.py`
- Modify: `tests/unit/test_context_builder.py`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_compaction.py`
- Modify: `tests/live/test_compaction_live.py`

**Interfaces produced:**

```python
class MemoryRetriever(Protocol):
    def retrieve(self, query: str) -> tuple[MemoryEntry, ...]: ...

class LocalMemoryRetriever:
    def __init__(
        self,
        store: MemoryStore,
        token_counter: TokenCounter,
        *,
        limit: int = 8,
        max_tokens: int = 2048,
    ) -> None: ...
```

- [x] **Step 1: 写排名和预算失败测试**

  覆盖：

  - 中文 CJK 二元组；
  - 拉丁词 token；
  - 完整子串最高权重；
  - identity/preference/constraint 稳定类别加权；
  - importance、时间衰减、`updated_at`、`memory_id` tie-break；
  - Top 8；
  - 2048 Token 截断；
  - 空 query 只返回少量高优先级稳定记忆；
  - Store 损坏时返回空结果而不是破坏 Agent Turn。

- [x] **Step 2: 运行 RED**

  Run: `py -3.12 -m pytest tests/unit/test_memory_retriever.py tests/unit/test_context_builder.py tests/unit/test_compaction.py -q`

  Expected: 当前 ContextBuilder 仍注入全部记忆。

- [x] **Step 3: 实现确定性 Retriever**

  使用纯本地评分。每次尝试加入一个候选时，把完整 `stable_memory` 文本包装为仅含一个 System Message 的 `LLMRequest`，调用现有 `TokenCounter.count()` 判断预算，避免另造不一致的字符估算。格式只包含：

  ```text
  Long-term memory:
  - [memory_id] content
  ```

  `ContextBuilder` 不再持有 `MemoryStore`，只持有 `MemoryRetriever | None`。query 取 Session 中最新 USER Message（含 steer）；无消息时传空字符串。Retriever 的 `StoreError` 降级为空。

- [x] **Step 4: 加配置并验证**

  新增：

  ```text
  MEMORY_RETRIEVAL_LIMIT=8
  MEMORY_CONTEXT_MAX_TOKENS=2048
  ```

  Run: `py -3.12 -m pytest tests/unit/test_memory_retriever.py tests/unit/test_context_builder.py tests/unit/test_compaction.py tests/unit/test_config.py -q`

  Expected: PASS；Compaction 的 stable context 只计算检索结果，不加载全库。

- [x] **Step 5: 提交**

  Commit: `feat: retrieve bounded relevant memories`

## Task 4: Strict LLM Extraction, Time Normalization, and Batch Apply

**Files:**

- Modify: `pyproject.toml`
- Create: `src/sjtuclaw/prompts/memory_extraction_prompt.md`
- Create: `src/sjtuclaw/runtime/memory_time.py`
- Create: `src/sjtuclaw/runtime/memory_extractor.py`
- Modify: `src/sjtuclaw/runtime/memory_service.py`
- Create: `tests/unit/test_memory_time.py`
- Create: `tests/unit/test_memory_extractor.py`
- Modify: `tests/unit/test_memory_service.py`

**Interfaces produced:**

```python
class MemoryOperationType(StrEnum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    NOOP = "NOOP"

class MemoryExtractor:
    async def extract(
        self,
        turn: MemoryTurn,
        *,
        rules: str,
        existing: tuple[MemoryEntry, ...],
    ) -> tuple[MemoryOperation, ...]: ...

class MemoryService:
    def apply_automatic_batch(
        self,
        operations: tuple[MemoryOperation, ...],
        *,
        source_session_id: str,
        source_message_id: str,
    ) -> tuple[MemoryEntry, ...]: ...
```

- [x] **Step 1: 加 `tzlocal>=5.4,<6` 并写时间测试**

  `tzlocal` 仅用于把 Windows/Unix 本机时区解析为 `zoneinfo.ZoneInfo`；业务测试注入 `ZoneInfo("Asia/Shanghai")`，不依赖开发机时区。

  覆盖 `今天/明天/后天`、`下周一..日`、可选明确时刻、跨月/跨年；“过段时间”“以后有机会”等保持 `original_time_text` 且 `event_at=None`。绝对 ISO 时间必须 timezone-aware，保存为 UTC，同时保存原 IANA 名称。

- [x] **Step 2: 写 Extractor 严格协议 RED 测试**

  使用 Fake LLM 覆盖：

  - 合法 `ADD/UPDATE/NOOP`；
  - 空响应、Markdown fence、多余顶层字段、非法枚举、非法 revision；
  - response 中含一个非法 operation 时整批拒绝；
  - `DELETE` 整批拒绝；
  - 自动来源字段由代码补齐，LLM 无权伪造；
  - 可解析时间由代码重新归一化，模糊时间不信任 LLM 猜值；
  - 含秘密的候选逐条丢弃，错误不含秘密原文。

- [x] **Step 3: 运行 RED**

  Run: `py -3.12 -m pytest tests/unit/test_memory_time.py tests/unit/test_memory_extractor.py tests/unit/test_memory_service.py -q`

  Expected: Extractor、Normalizer 和自动 batch 尚不存在。

- [x] **Step 4: 实现一次调用的 Extractor**

  LLMRequest 只含两个 Message：

  1. 包内 extractor system prompt；
  2. 紧凑 JSON 输入：当前时间、本机时区、用户规则、当前用户消息、最终 Assistant Answer、本 Turn Tool Result、少量相关既有 Memory。

  不包含 Assistant reasoning。严格用 `json.loads`，不修复 Markdown、不截取疑似 JSON。限制规则、Turn 文本、Tool Result、既有候选数和 operation 数。模型响应只被解析为候选，最终由 `MemoryService` 再校验。

- [x] **Step 5: 实现全批次应用和冲突保护**

  `apply_automatic_batch` 在 Memory lock 中重新加载当前文档，先验证全部 operation，再构造完整新 Memory，最后一次 `replace()`。任一 operation 无效则零写入。UPDATE 必须匹配 `memory_id + revision`。

  自动 UPDATE 用当前 Turn 更新 `source_kind/source_session_id/source_message_id`，表示最新自动事实的直接来源；保留 `memory_id/created_at`，递增 revision。用户手动编辑只改允许字段，保留原来源。

  如果抽取期间 revision 已变化：

  - 在锁内重新读取一次；
  - 仍不匹配则整批丢弃；
  - 不重跑 LLM，不覆盖用户刚修改的内容；
  - 因而仍满足“每 Turn 最多一次额外模型调用”。

- [x] **Step 6: 运行 focused tests 和静态检查**

  Run: `py -3.12 -m pytest tests/unit/test_memory_time.py tests/unit/test_memory_extractor.py tests/unit/test_memory_service.py -q`

  Run: `py -3.12 -m ruff check src/sjtuclaw/runtime/memory_time.py src/sjtuclaw/runtime/memory_extractor.py src/sjtuclaw/runtime/memory_service.py`

  Expected: PASS。

- [x] **Step 7: 提交**

  Commit: `feat: extract safe memory operations from turns`

## Task 5: Silent Post-Answer Capture and Application Wiring

**Files:**

- Create: `src/sjtuclaw/runtime/memory_capture.py`
- Modify: `src/sjtuclaw/runtime/agent_loop.py`
- Modify: `src/sjtuclaw/runtime/__init__.py`
- Modify: `src/sjtuclaw/runtime/session_service.py`
- Modify: `src/sjtuclaw/bootstrap.py`
- Modify: `src/sjtuclaw/entrypoints/cli.py`
- Modify: `tests/unit/test_agent_loop.py`
- Modify: `tests/unit/test_session_service.py`
- Modify: `tests/unit/test_cli_commands.py`
- Modify: `tests/unit/test_bootstrap_tools.py`
- Modify: `tests/unit/test_archive_service.py`
- Modify: `tests/integration/test_gateway_archive.py`
- Modify: `tests/integration/test_gateway_runtime.py`
- Modify: `tests/integration/test_gateway_scheduler.py`
- Modify: `tests/integration/test_gateway_security.py`
- Modify: `tests/integration/test_session_management_queue.py`
- Modify: `tests/live/test_compaction_live.py`

**Interfaces produced:**

```python
class MemoryCapture:
    async def capture(self, session: Session, request: RuntimeRequest) -> None: ...

@dataclass(frozen=True, slots=True)
class RuntimeApplication:
    ...
    memory_service: MemoryService
    memory_policy: MemoryPolicyService
```

- [x] **Step 1: 写触发边界和顺序失败测试**

  覆盖：

  - CLI/Gateway 成功 Turn 调用一次 capture；
  - Scheduler 不调用；
  - Assistant Message 已保存且 `ASSISTANT_MESSAGE` 已 emit 后才 capture；
  - capture 抛 LLM/Store/解析异常时 Run 仍成功；
  - 不发 Memory Event；
  - approval pause/resume 后只捕获一次；
  - 历史中存在重复用户文本和 steer 时，来源仍是当前 Run 前最新的普通 USER Message。

- [x] **Step 2: 运行 RED**

  Run: `py -3.12 -m pytest tests/unit/test_agent_loop.py -k memory -q`

  Expected: AgentLoop 没有 MemoryCapture。

- [x] **Step 3: 实现来源组装和静默失败**

  `MemoryCapture` 从已完成 Session 中定位：

  - 当前 Run 前最新 `role=USER, input_kind=USER` 的 Message 作为 provenance；
  - 该 Message 后至最终 Assistant Message 的 Tool Result；
  - 当前最终 Assistant Answer。

  同 Session 已由现有 SessionCoordinator 串行，后续 queued request 尚未追加 USER Message，因此无需改变 Session schema 或 RunCheckpoint。对 duplicate text、steer、approval resume 写回归测试固定此假设。

  Capture 内部捕获预期异常，只记录不含候选正文的安全诊断；不把错误抛回 AgentLoop。

- [x] **Step 4: 在 AgentLoop 的正确位置接入**

  固定顺序：

  ```text
  append Assistant -> close tool context -> mark Run completed
  -> save Session -> emit ASSISTANT_MESSAGE
  -> await MemoryCapture.capture
  -> return usage
  ```

  仅 `InvocationSource.CLI/GATEWAY` 执行。不得添加 `RuntimeEventType`。

- [x] **Step 5: 重组 bootstrap 和 CLI**

  - 先创建 ProviderRuntime，再创建 Retriever/ContextBuilder；
  - 组装 Memory Store、Policy、Service、Extractor、Capture；
  - `SessionService` 删除 memory 责任和 constructor 参数；
  - CLI `/memory list/add/delete` 改用 `application.memory_service`，输出行为保持兼容；
  - `RuntimeApplication` 暴露 Memory Service/Policy 给 Gateway。

- [x] **Step 6: 运行 runtime 回归**

  Run: `py -3.12 -m pytest tests/unit/test_agent_loop.py tests/unit/test_context_builder.py tests/unit/test_session_service.py tests/unit/test_cli_commands.py tests/unit/test_bootstrap_tools.py tests/unit/test_archive_service.py tests/integration/test_gateway_runtime.py tests/integration/test_gateway_archive.py tests/integration/test_gateway_scheduler.py tests/integration/test_gateway_security.py tests/integration/test_session_management_queue.py -q`

  Expected: PASS；没有 live LLM 调用。

- [x] **Step 7: 提交**

  Commit: `feat: capture memories after successful user turns`

## Task 6: Authenticated Gateway Memory and Policy APIs

**Files:**

- Modify: `src/sjtuclaw/errors.py`
- Modify: `src/sjtuclaw/entrypoints/gateway.py`
- Create: `tests/integration/test_gateway_memory.py`
- Modify: `tests/integration/test_gateway_security.py`
- Modify: `tests/unit/test_gateway_startup.py`

**Request bodies:**

```json
POST /memories
{
  "content": "用户偏好中文回答",
  "category": "preference",
  "originalTimeText": null,
  "eventAt": null,
  "eventTimezone": null
}

PATCH /memories/{id}
{
  "revision": 2,
  "content": "用户偏好简洁中文回答",
  "category": "preference",
  "originalTimeText": null,
  "eventAt": null,
  "eventTimezone": null
}

PUT /memory-policy
{
  "revision": 1,
  "rules": "..."
}

POST /memory-policy/reset
{
  "revision": 2
}
```

- [x] **Step 1: 写所有 API 的失败集成测试**

  覆盖 auth Cookie、分页、query、manual source、PATCH/DELETE revision、Policy override/reset、camelCase payload、未知字段、秘密过滤。

- [x] **Step 2: 写错误映射测试**

  - stale revision -> `409 MEMORY_REVISION_CONFLICT`；
  - 参数/规则/秘密无效 -> `422`；
  - `StoreError`/损坏文件 -> `503 MEMORY_STORE_UNAVAILABLE`；
  - 未认证 -> 保持当前 auth 响应；
  - 错误正文不含被过滤的秘密。

- [x] **Step 3: 运行 RED**

  Run: `py -3.12 -m pytest tests/integration/test_gateway_memory.py tests/integration/test_gateway_security.py tests/unit/test_gateway_startup.py -q`

  Expected: 路由和 `create_gateway(..., memories=..., memory_policy=...)` 参数不存在。

- [x] **Step 4: 实现 Pydantic body、序列化和路由**

  使用 `ConfigDict(populate_by_name=True, extra="forbid")`。GET 默认 limit 50，最大 100。API 不允许修改 `memoryId/source*/createdAt`。POST importance 固定默认 3；自动 importance 只来自受校验的 Extractor。

  GET policy 返回：

  ```json
  {
    "rules": "...",
    "defaultRules": "...",
    "isOverride": false,
    "revision": 1,
    "updatedAt": null
  }
  ```

- [x] **Step 5: 验证无 SSE 变化**

  在现有 Gateway runtime 测试中断言成功自动捕获不产生新的用户可见 event type，既有 event 顺序不变。

  Run: `py -3.12 -m pytest tests/integration/test_gateway_memory.py tests/integration/test_gateway_runtime.py tests/integration/test_gateway_security.py tests/unit/test_gateway_startup.py -q`

  Expected: PASS。

- [x] **Step 6: 提交**

  Commit: `feat: expose local memory management APIs`

## Task 7: Shared Top Utility Toolbar and Stable Workbench Layout

**Files:**

- Modify: `web/src/App.tsx`
- Modify: `web/src/SchedulerView.tsx`
- Modify: `web/src/styles.css`

**UI contract:**

```text
Left Session column | Utility toolbar spanning center + right
                    | Chat/Scheduler center | Activity right
```

- [x] **Step 1: 记录现有 UI 回归基线**

  手动核对并截图：

  - 普通会话搜索；
  - 已归档会话搜索；
  - Scheduler 入口；
  - Settings popover；
  - details/activity drawer；
  - 1280px、900px、390px 三种宽度。

  不修改 `.superpowers/brainstorm` 中已批准的示意图。

- [x] **Step 2: 实现两行 CSS Grid**

  `.workbench` 改为两行：utility row + content row。Session drawer 占第一列两行；toolbar 占第二至第三列首行；Chat/Details 占第二行。折叠中栏/右栏时保持现有语义。

  在窄屏中 toolbar 固定顶部、按钮可横向滚动或压缩为短标签；Session/Details 仍使用现有 drawer 行为。

- [x] **Step 3: 集中小按钮**

  Toolbar 右对齐：

  ```text
  记忆 | 定时任务 | 已归档会话 | 设置
  ```

  - Scheduler 按钮切换 Chat/Scheduler，激活时再次点击返回 Chat；
  - 已归档会话按钮进入 Chat 并调用现有 archive list 行为；
  - Settings popover 从左栏移动到 toolbar，状态/API 设置逻辑不改；
  - 左栏保留普通和归档 Session 搜索，不把它们换成 Memory 搜索；
  - SchedulerView 嵌入 toolbar 下方的中+右区域，删除重复“返回会话”小按钮。

- [x] **Step 4: 构建验证**

  Run: `Set-Location web; npm run build`

  Expected: TypeScript/Vite PASS，无 DOM nesting 或未使用变量错误。

- [x] **Step 5: 提交**

  Commit: `feat: add shared workbench utility toolbar`

## Task 8: Memory Timeline Drawer, Search, Edit, and Delete

**Files:**

- Create: `web/src/memory.ts`
- Create: `web/src/MemoryDrawer.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

**TypeScript interfaces:**

```typescript
export type MemoryEntry = {
  memoryId: string;
  content: string;
  category: MemoryCategory;
  importance: number;
  sourceKind: "automatic" | "manual";
  sourceSessionId: string | null;
  sourceMessageId: string | null;
  originalTimeText: string | null;
  eventAt: string | null;
  eventTimezone: string | null;
  createdAt: string;
  updatedAt: string;
  revision: number;
};
```

- [x] **Step 1: 建立抽屉状态和只读时间线**

  打开 Memory 时请求 `/memories?limit=50`。抽屉桌面宽约 390px，固定右侧、位于 toolbar 下方，覆盖 activity 和部分 chat；移动端 `width: min(100vw, 390px)`。聊天保持可见/可滚动，关闭恢复 activity。

- [x] **Step 2: 实现时间节点和时间显示**

  - 纵向连接线；
  - 每条 Memory 一个 dot/node；
  - 按浏览器本地日期/月分组；
  - category 使用低饱和 CSS 变量；
  - `eventAt` 存在时标“事件时间”，否则标“记录时间”；
  - 同时显示 `Intl.RelativeTimeFormat` 相对时间与 `Intl.DateTimeFormat` 精确时间；
  - 保留并显示 `originalTimeText`；
  - 未来事件允许排在今天之前。

- [x] **Step 3: 实现独立 Memory 搜索和分页**

  Memory 搜索只请求 `/memories?query=...`，不能写入 `sessionQuery` 或 `archivedSessionQuery`。新 query 取消旧请求并重置 cursor；“加载更多”追加且按 `memoryId` 去重。

- [x] **Step 4: 实现抽屉内编辑**

  节点展开表单只允许 content/category/event time。`datetime-local` 转 ISO UTC，同时发送浏览器 IANA timezone。保存使用当前 revision；成功替换节点。409 时保留草稿、刷新服务端节点并提示“记忆已变化，请核对后重试”。

- [x] **Step 5: 实现永久删除**

  使用一次明确确认，DELETE 带 revision。成功后移除节点；409 刷新；失败不乐观删除。

- [x] **Step 6: Build 和键盘/无障碍检查**

  Run: `Set-Location web; npm run build`

  手动检查 Esc/关闭按钮、tab 顺序、`aria-current/aria-expanded/role=dialog`、焦点可见、空态、loading、error。

- [x] **Step 7: 提交**

  Commit: `feat: add editable memory timeline drawer`

## Task 9: Memory Rules Tab and Non-disruptive Health State

**Files:**

- Modify: `web/src/MemoryDrawer.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`
- Modify: `tests/integration/test_gateway_memory.py`

- [x] **Step 1: 实现“记忆规则”页签**

  打开页签时 GET policy。多行自然语言编辑器显示当前有效 rules、override 标记和 revision。保存 PUT；成功后更新 snapshot。失败保留未提交草稿，旧规则继续生效。

- [x] **Step 2: 实现恢复默认**

  点击后先展示 `defaultRules` 预览并确认，再 POST reset 带 revision。成功切换 `isOverride=false`。不回溯重处理已有 Memory。

- [x] **Step 3: 实现 Memory 健康状态**

  App 启动后后台调用 `GET /memories?limit=1`：

  - 成功：Memory 按钮正常；
  - 503：按钮显示不打扰的错误圆点和 tooltip；
  - 打开抽屉：显示持久错误说明和重试按钮；
  - 不弹 Toast，不影响 Chat/Scheduler；
  - 网络短暂错误可重试，成功后清除状态。

  不新增 health SSE 或轮询；打开抽屉和用户点击重试时刷新即可。

- [x] **Step 4: 前后端验证**

  Run: `py -3.12 -m pytest tests/integration/test_gateway_memory.py -q`

  Run: `Set-Location web; npm run build`

  Expected: PASS。

- [x] **Step 5: 提交**

  Commit: `feat: manage memory rules and health state`

## Task 10: Documentation, Structure Map, and Full Verification

**Files:**

- Modify: `README.md`
- Modify: `project_structure.md`
- Modify: `.env.example` if Task 3 did not already document every setting
- Modify: implementation plan checkboxes/status only after each command actually passes

- [x] **Step 1: 更新用户文档**

  说明：

  - 自动记忆何时运行/不运行；
  - 默认静默且无开关；
  - 记忆规则只影响未来；
  - 时间线搜索、编辑、永久删除；
  - 数据文件和 v1->v2 惰性迁移；
  - 本地轻量检索及未来 RAG 扩展点；
  - Store 损坏时 Chat 降级和排查方式。

- [x] **Step 2: 更新结构文档**

  在 `project_structure.md` 中加入所有新增 Python/React 文件，并把 SessionService 的 Memory 职责移到 `MemoryService/Policy/Extractor/Retriever/Capture`。

- [x] **Step 3: 运行全部非 live 测试**

  Run: `py -3.12 -m pytest -m "not live"`

  Expected: 全部 PASS；数量不低于基线 292。

- [x] **Step 4: 运行静态检查**

  Run: `py -3.12 -m ruff check . --no-cache`

  Run: `py -3.12 -m mypy src --no-incremental`

  Expected: 两者 PASS。

- [x] **Step 5: 审计完整 mypy 基线**

  Run: `py -3.12 -m mypy src tests --no-incremental`

  Expected: 若既有 13 个测试类型错误仍存在，确认没有新增错误，特别是本计划新建/修改的测试文件零新增。不要为清零基线做无关重构。

- [x] **Step 6: 前端生产构建**

  Run: `Set-Location web; npm run build`

  Expected: PASS。

- [x] **Step 7: 本地手工验收**

  用临时 `SJTUCLAW_DATA_DIR` 启动 Gateway，避免污染真实数据。至少验证：

  1. 普通和归档 Session 搜索仍工作；
  2. toolbar 的四个入口都可用；
  3. 创建一条 manual Memory；
  4. 运行一个包含稳定偏好的 Gateway Turn，主回答先出现，之后重新打开 Memory 可看到自动节点；
  5. Scheduler Turn 不增加 Memory；
  6. 修改/冲突/删除 Memory；
  7. 修改规则、失败保留草稿、恢复默认；
  8. 相对和精确时间、事件/记录时间标签；
  9. 损坏临时 memory.json 后 Chat 可继续、按钮显示错误、文件未被覆盖；
  10. UI 不出现逐轮记忆通知。

- [x] **Step 8: 最终 diff 和安全检查**

  Run: `git status --short`

  Run: `git diff --check`

  Run: `git diff --stat HEAD~10..HEAD`

  人工检查无 `.env`、API Key、临时 data dir、截图、`web/dist`、`.superpowers` 产物被提交。

- [x] **Step 9: 最终提交**

  Commit: `docs: document automatic long-term memory`

## Definition of Done

- 自动抽取、轻量检索、规则、API、时间线 UI 接入同一 Runtime。
- v1 安全升级 v2；主回答优先；Scheduler 不写记忆。
- 默认检索 Top 8 / 2048 tokens，来源、秘密和 revision 约束有效。
- 工具栏、右侧抽屉、Memory 搜索和既有会话搜索符合已批准布局。
- 测试、ruff、`mypy src`、Vite build 和手工验收通过。
