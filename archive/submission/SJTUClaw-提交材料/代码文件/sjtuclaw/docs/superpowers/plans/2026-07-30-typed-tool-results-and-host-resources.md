# Typed Tool Results and Host Resources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 为全部 Tool 建立强制输出契约；下载等操作资源只交给 Host 渲染，模型只看到业务结果，不再接触或猜测下载地址。

**Architecture:** Tool 返回统一的 `ToolExecutionOutput`。Registry 校验 `structured_content` 和资源类型；Runtime/持久化保留完整结果；LLM 适配层只投影 `content + structured_content`；Gateway 将资源交给 Web Host，前端按通用资源结构渲染操作。

**Tech Stack:** Python 3.12、dataclasses、JSON Schema Draft 2020-12、pytest、mypy、ruff、FastAPI、React 19、TypeScript 7、Vitest、Vite。

## Global Constraints

- 以已确认设计为唯一范围依据：`docs/superpowers/specs/2026-07-30-typed-tool-results-and-host-resources-design.md`。
- 不引入 S3、`PUBLIC_BASE_URL`、完整 MCP 协议、全局 Markdown URL 来源校验。
- 不修改 `/downloads/{download_id}` 的既有路由和下载存储实现。
- 不依赖安装目录中的 `system_prompt.md` 更新；规则必须由 LLM 适配层稳定注入，兼容已有安装。
- 保留现有 Tool 的人类可读 `content` 语义；新增机器可读 `structured_content`。
- `resources` 永不进入模型请求；只允许进入持久化、Gateway 和 Host。
- 下载资源的 `href` 只能是同源相对路径 `/downloads/<id>`。
- 当前工作树已有用户改动：
  - `README.md`
  - `src/sjtuclaw/infrastructure/tools/advanced.py`
  - `tests/unit/test_advanced_tools.py`
  - `web/src/App.tsx`
  - `docs/design.md`
- 实施前先保存 `git diff` 基线。修改重叠文件时必须保留已有改动，尤其是：
  - `advanced.py` 的 `create_parents=True` 默认行为和 `"."` 工作区支持；
  - `test_advanced_tools.py` 对上述行为的测试；
  - `App.tsx` 的“添加文本 / PDF”附件改动。
- 不得用 `git add <重叠文件>` 直接暂存整个文件。提交前使用选择性暂存，并用 `git diff --cached` 确认没有带入用户原有改动；无法安全分离时，保留实施改动为未提交状态并报告。
- 新增 `web/src/toolResources.ts` 后，必须同步更新根目录 `project_structure.md`。

---

## Task 1: 建立 Tool 输出领域契约

**Files:**

- Modify: `src/sjtuclaw/models/tool.py`
- Modify: `tests/unit/test_tool_models.py`

### 1.1 先写领域模型失败测试

- [x] 在 `tests/unit/test_tool_models.py` 增加以下覆盖：
  - `ToolExecutionOutput` 冻结 `structured_content`，不保留调用方可变引用；
  - `ToolResourceLink` 拒绝空 `kind`、空 `name`、绝对 URL 和非 `/` 开头的 `href`；
  - `ToolResult` 成功时保留结构化结果和资源；
  - `ToolResult` 错误时不允许携带资源。

测试使用的精确对象形状：

```python
resource = ToolResourceLink(
    kind="download",
    name="report.md",
    href="/downloads/download_123",
    mime_type="text/markdown",
    expires_at="2026-07-31T00:00:00+00:00",
)
output = ToolExecutionOutput(
    content="下载快照已创建。",
    structured_content={"filename": "report.md", "size_bytes": 12},
    resources=(resource,),
)
```

- [x] 运行：

```powershell
python -m pytest tests/unit/test_tool_models.py -q
```

预期：导入 `ToolExecutionOutput`、`ToolResourceLink` 失败。

### 1.2 实现不可变结果类型

- [x] 在 `src/sjtuclaw/models/tool.py` 增加：

```python
@dataclass(frozen=True, slots=True)
class ToolResourceLink:
    kind: str
    name: str
    href: str
    mime_type: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionOutput:
    content: str
    structured_content: Mapping[str, JsonValue]
    resources: tuple[ToolResourceLink, ...] = ()
```

具体校验要求：

- `kind`、`name`、`content` 必须是字符串；`kind`、`name` 不得为空。
- `href` 必须以单个 `/` 开头，不得以 `//` 开头，不得包含 scheme 或 host。
- `structured_content` 使用现有 `freeze_json()` 冻结，且顶层必须为对象。
- `resources` 转为 tuple。

- [x] 扩展 `ToolResult`：

```python
structured_content: Mapping[str, JsonValue] | None = None
resources: tuple[ToolResourceLink, ...] = ()
```

约束：

- 成功结果允许 `structured_content=None`，用于读取旧会话；Task 2 完成后，Registry 不再产生这种成功结果。
- 错误结果允许 `structured_content=None`，且 `resources` 必须为空。
- 非空结构化内容使用 `freeze_json()` 冻结。
- `resources` 转为 tuple。

### 1.4 验证 Task 1

- [x] 运行：

```powershell
python -m pytest tests/unit/test_tool_models.py -q
python -m ruff check src/sjtuclaw/models/tool.py tests/unit/test_tool_models.py
python -m mypy src/sjtuclaw/models/tool.py tests/unit/test_tool_models.py
```

预期：全部通过。

- [x] 只在能够排除用户原有改动时提交：

```powershell
git commit -m "feat: add typed tool output contracts"
```

---

## Task 2: 迁移 11 个内置 Tool，并在 Registry 严格校验

**Files:**

- Modify: `src/sjtuclaw/infrastructure/tools/registry.py`
- Modify: `src/sjtuclaw/infrastructure/tools/readonly.py`
- Modify: `src/sjtuclaw/infrastructure/tools/advanced.py`
- Modify: `src/sjtuclaw/infrastructure/tools/skills.py`
- Modify: `src/sjtuclaw/infrastructure/tools/skill_selection.py`
- Modify: `tests/unit/test_tool_registry.py`
- Modify: `tests/unit/test_tool_readonly.py`
- Modify: `tests/unit/test_read_attachment_tool.py`
- Modify: `tests/unit/test_advanced_tools.py`
- Modify: `tests/unit/test_read_skill_resource_tool.py`
- Modify: `tests/unit/test_skill_routing.py`
- Modify: `tests/unit/test_agent_loop.py`
- Modify: `tests/unit/test_deepseek_llm.py`
- Modify: `tests/unit/test_deepseek_token_counter.py`
- Modify: `tests/unit/test_provider_adapters.py`
- Modify: `tests/unit/test_token_counter.py`
- Modify: `tests/unit/test_tool_protocol.py`

### 2.1 先写输出 Schema 与资源失败测试

- [x] 在 `tests/unit/test_tool_registry.py` 增加三个 Tool fixture：
  - 返回不符合 `output_schema` 的 `structured_content`；
  - 返回声明之外的资源类型；
  - 返回 `download` 资源但 `href` 不匹配 `/downloads/<id>`。
  - 返回超过 16 个资源，或超过资源字段长度上限的资源。

- [x] 断言三种情况都得到：

```python
ToolResult(
    ...,
    content="tool returned an invalid result",
    status=ToolResultStatus.ERROR,
    error_code="TOOL_OUTPUT_SCHEMA_INVALID",
)
```

并断言 `resources == ()`、`structured_content is None`。

- [x] 在 `tests/unit/test_advanced_tools.py` 增加 `create_download` 契约测试：
  - `output.content` 不包含 `download_id`；
  - `output.structured_content` 不包含 `download_id` 和 `href`；
  - `output.resources` 有一个 `kind="download"`；
  - `href == f"/downloads/{snapshot.download_id}"`；
  - 资源包含文件名、MIME、过期时间。

- [x] 运行：

```powershell
python -m pytest tests/unit/test_tool_registry.py tests/unit/test_advanced_tools.py -q
```

预期：Registry 尚未校验输出，且 Tool 仍返回字符串，测试失败。

### 2.2 在 Registry 编译并执行输出校验

- [x] 扩展 `ToolDefinition`。字段顺序以绑定设计为准；现有位置参数调用必须改为关键字参数，避免将既有 `safety_level` 误传入新的必填字段：

```python
name: str
description: str
input_schema: Mapping[str, JsonValue]
output_schema: Mapping[str, JsonValue]
result_usage: str
safety_level: ToolSafetyLevel
allowed_resource_kinds: tuple[str, ...] = ()
```

具体校验要求：

- `output_schema` 顶层必须为对象并由 `freeze_json()` 冻结。
- `result_usage` 不得为空。
- `allowed_resource_kinds` 不得包含空值或重复值。

- [x] 将 `ToolHandler.execute()` 的返回类型由 `str` 改为 `ToolExecutionOutput`。

- [x] 在 `ToolRegistry.__init__()` 中同时执行：

```python
Draft202012Validator.check_schema(mutable_json(definition.output_schema))
```

并保存每个 Tool 的输出 validator。

- [x] `execute_batch()` 获得 `ToolExecutionOutput` 后按顺序校验：
  1. `structured_content` 符合 `output_schema`；
  2. 每个 `resource.kind` 位于 `allowed_resource_kinds`；
  3. `download` 的 `href` 匹配 `^/downloads/[A-Za-z0-9_-]{1,256}$`；
  4. 所有资源 `href` 为站内相对路径。

- [x] 资源上限固定为：单次结果最多 16 个；`kind` 最多 64 字符，`name` 与
  `mime_type` 最多 255 字符，`expires_at` 最多 64 字符。`href` 不得含控制字符、
  反斜杠、fragment 或路径穿越；当前唯一允许的资源类型为 `download`，其正则已
  同时限制 `href` 长度与编码。

- [x] 任一校验失败，转换为 `TOOL_OUTPUT_SCHEMA_INVALID`，不泄露内部 validator 细节，不把资源带入错误结果。

- [x] 成功时按以下方式构造结果：

```python
ToolResult(
    call_id=intent.call_id,
    tool_name=intent.name,
    content=output.content,
    structured_content=output.structured_content,
    resources=output.resources,
)
```

### 2.3 迁移所有 Tool 声明和返回值

- [x] 每个 `ToolDefinition` 都显式声明 `output_schema`、`result_usage`。只有 `create_download` 声明：

```python
allowed_resource_kinds=("download",)
```

- [x] 11 个 Tool 的精确结构化字段如下：

| Tool | `structured_content` 字段 |
|---|---|
| `current_time` | `iso8601` |
| `list_dir` | `path`, `entries`；每个 entry 含 `name`, `type` |
| `read_file` | `path`, `size_bytes` |
| `read_attachment` | `attachment_id`, `truncated`, `next_cursor` |
| `copy_attachment_to_workspace` | `attachment_id`, `destination`, `size_bytes`, `sha256`, `overwritten` |
| `write_file` | `path`, `size_bytes`, `sha256`, `overwritten` |
| `create_download` | `filename`, `size_bytes`, `sha256`, `expires_at` |
| `new_shell` | `cwd`, `workspace_revision`, `replaced_existing` |
| `run_command` | `command`, `cwd_before`, `cwd_after`, `exit_code`, `stdout`, `stderr`, `timed_out`, `truncated` |
| `read_skill_resource` | `skill_id`, `path`, `truncated`, `next_cursor` |
| `select_skill` | `skill_id`, `snapshot_id`, `version` |

所有字段默认必填。统一用以下 helper 生成顶层 Schema：

```python
def _output_schema(properties: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": tuple(properties),
        "properties": properties,
    }
```

字段类型固定如下：

| Tool | 字段 JSON Schema |
|---|---|
| `current_time` | `iso8601: string, format date-time` |
| `list_dir` | `path: string`; `entries: array`，item 为禁止额外字段的对象，必填 `name: string`, `type: enum["file","directory"]` |
| `read_file` | `path: string`, `size_bytes: integer, minimum 0` |
| `read_attachment` | `attachment_id: string`, `truncated: boolean`, `next_cursor: string\|null` |
| `copy_attachment_to_workspace` | `attachment_id: string`, `destination: string`, `size_bytes: integer, minimum 0`, `sha256: string, pattern "^[0-9a-f]{64}$"`, `overwritten: boolean` |
| `write_file` | `path: string`, `size_bytes: integer, minimum 0`, `sha256: string, pattern "^[0-9a-f]{64}$"`, `overwritten: boolean` |
| `create_download` | `filename: string`, `size_bytes: integer, minimum 0`, `sha256: string, pattern "^[0-9a-f]{64}$"`, `expires_at: string, format date-time` |
| `new_shell` | `cwd: string`, `workspace_revision: integer, minimum 1`, `replaced_existing: boolean` |
| `run_command` | `command: string`, `cwd_before: string`, `cwd_after: string\|null`, `exit_code: integer\|null`, `stdout: string`, `stderr: string`, `timed_out: boolean`, `truncated: boolean`；超时时 shell 已终止，`cwd_after` 必须为 `null`，不得伪称为原目录 |
| `read_skill_resource` | `skill_id: string`, `path: string`, `truncated: boolean`, `next_cursor: string\|null` |
| `select_skill` | `skill_id: string`, `snapshot_id: string`, `version: string` |

可空字段使用 Draft 2020-12 联合类型，例如：

```python
"exit_code": {"type": ("integer", "null")}
```

- [x] 现有 JSON 型 Tool 的 `content` 继续使用紧凑 JSON 文本，避免改变模型可读语义；正文只保留在 `content`，`structured_content` 只保留元数据，禁止重复大段文本。

- [x] `current_time`、`list_dir`、`read_file` 保留当前纯文本 `content`，同时补充结构化字段。

- [x] `create_download` 返回：

```python
data = {
    "filename": snapshot.filename,
    "size_bytes": snapshot.size_bytes,
    "sha256": snapshot.sha256,
    "expires_at": snapshot.expires_at,
}
return ToolExecutionOutput(
    content="下载快照已创建，Host 会显示可用的下载操作；不要构造或猜测下载链接。",
    structured_content=data,
    resources=(
        ToolResourceLink(
            kind="download",
            name=snapshot.filename,
            href=f"/downloads/{snapshot.download_id}",
            mime_type=mimetypes.guess_type(snapshot.filename)[0]
            or "application/octet-stream",
            expires_at=snapshot.expires_at,
        ),
    ),
)
```

- [x] 保留 `advanced.py` 中用户已有的 `create_parents` 默认值和 `"."` 路径行为，不回退这些代码。

- [x] 更新 `rg -l "ToolDefinition\(" src tests` 找到的全部测试 fixture，为它们补齐精确的 `output_schema` 和 `result_usage`。Token counter 测试只补声明字段，不改变计数断言之外的行为。

- [x] 将 `test_agent_loop.py`、`test_tool_registry.py` 中的 Recording Tool 改为返回 `ToolExecutionOutput`；其他测试直接调用内置 Tool 时按 2.4 更新断言。

### 2.4 更新各 Tool 单测

- [x] 将原先对返回字符串的断言改为：
  - `result.content` 保持原语义；
  - `mutable_json(result.structured_content)` 等于预期字典；
  - 非资源 Tool 的 `result.resources == ()`。

- [x] `select_skill` 的 Runtime 特殊路径暂不在本 Task 修改；这里只验证 `SelectSkillTool.execute()` 的输出契约。

### 2.5 验证 Task 2

- [x] 运行：

```powershell
python -m pytest tests/unit/test_tool_registry.py tests/unit/test_tool_readonly.py tests/unit/test_read_attachment_tool.py tests/unit/test_advanced_tools.py tests/unit/test_read_skill_resource_tool.py tests/unit/test_skill_routing.py tests/unit/test_agent_loop.py tests/unit/test_deepseek_llm.py tests/unit/test_deepseek_token_counter.py tests/unit/test_provider_adapters.py tests/unit/test_token_counter.py tests/unit/test_tool_protocol.py -q
python -m ruff check src/sjtuclaw/infrastructure/tools tests/unit/test_tool_registry.py tests/unit/test_tool_readonly.py tests/unit/test_read_attachment_tool.py tests/unit/test_advanced_tools.py tests/unit/test_read_skill_resource_tool.py tests/unit/test_skill_routing.py tests/unit/test_agent_loop.py tests/unit/test_deepseek_llm.py tests/unit/test_deepseek_token_counter.py tests/unit/test_provider_adapters.py tests/unit/test_token_counter.py tests/unit/test_tool_protocol.py
python -m mypy src/sjtuclaw/infrastructure/tools tests/unit/test_tool_registry.py tests/unit/test_tool_readonly.py tests/unit/test_read_attachment_tool.py tests/unit/test_advanced_tools.py tests/unit/test_read_skill_resource_tool.py tests/unit/test_skill_routing.py tests/unit/test_agent_loop.py tests/unit/test_deepseek_llm.py tests/unit/test_deepseek_token_counter.py tests/unit/test_provider_adapters.py tests/unit/test_token_counter.py tests/unit/test_tool_protocol.py
```

预期：全部通过。

- [x] 选择性暂存，确认没有包含 `advanced.py` 和 `test_advanced_tools.py` 的用户原有差异，再提交：

```powershell
git diff --cached
git commit -m "feat: validate structured outputs for all tools"
```

---

## Task 3: 持久化完整结果，并建立模型安全投影

**Files:**

- Modify: `src/sjtuclaw/models/message.py`
- Modify: `src/sjtuclaw/runtime/agent_loop.py`
- Modify: `src/sjtuclaw/infrastructure/persistence/json_store.py`
- Modify: `src/sjtuclaw/infrastructure/llm/tool_protocol.py`
- Modify: `src/sjtuclaw/infrastructure/llm/deepseek.py`
- Modify: `src/sjtuclaw/infrastructure/llm/anthropic.py`
- Modify: `src/sjtuclaw/infrastructure/tokenizers/deepseek.py`
- Modify: `src/sjtuclaw/infrastructure/tokenizers/approximate.py`
- Modify: `tests/unit/test_tool_messages.py`
- Modify: `tests/unit/test_agent_loop.py`
- Modify: `tests/unit/test_json_store.py`
- Modify: `tests/contract/test_json_store_contract.py`
- Modify: `tests/unit/test_tool_protocol.py`
- Modify: `tests/unit/test_deepseek_llm.py`
- Modify: `tests/unit/test_provider_adapters.py`
- Modify: `tests/unit/test_deepseek_token_counter.py`
- Modify: `tests/unit/test_token_counter.py`

### 3.1 先写 Message、持久化与模型投影失败测试

- [x] 在 `tests/unit/test_tool_messages.py` 测试：
  - TOOL 消息允许 `tool_structured_content` 和 `tool_resources`；
  - 非 TOOL 消息拒绝这些字段；
  - 错误 TOOL 消息拒绝资源。

- [x] 在 `tests/unit/test_json_store.py` 增加 v9 往返测试：
  - 保存并读取含下载资源的 TOOL 消息，所有字段不丢失；
  - 读取 v8 fixture 时，`tool_structured_content is None`、`tool_resources == ()`。

- [x] 在 `tests/unit/test_tool_protocol.py` 增加：
  - 新格式 TOOL 消息投影为仅含 `content` 与 `structured_content` 的规范 JSON；
  - 输出中完全不含 `resources`、`href`、`download_id`；
  - 旧格式 TOOL 消息继续输出原始 `content`；
  - JSON 协议提示包含每个 Tool 的 `output_schema`、`result_usage`；
  - Native 协议注入共享结果使用规则。

- [x] 在 DeepSeek、Anthropic 适配器测试中断言：
  - 请求包含稳定的共享规则；
  - Tool 结果不携带资源字段；
  - 两种 Provider 使用同一投影语义。

- [x] 运行：

```powershell
python -m pytest tests/unit/test_tool_messages.py tests/unit/test_json_store.py tests/unit/test_tool_protocol.py tests/unit/test_deepseek_llm.py tests/unit/test_provider_adapters.py -q
```

预期：新字段、v9 schema 和安全投影尚不存在，测试失败。

### 3.2 扩展 Message 与 AgentLoop

- [x] 在 `Message` 增加仅供 TOOL 消息使用的字段：

```python
tool_structured_content: Mapping[str, JsonValue] | None = None
tool_resources: tuple[ToolResourceLink, ...] = ()
```

校验与 `ToolResult` 一致，并限制这些字段只能用于 `MessageRole.TOOL`。

- [x] 在 `AgentLoop._persist_results()` 将 `ToolResult` 的两个字段复制到 `Message`，并加入 Runtime `TOOL_RESULT` payload：

```python
"structured_content": mutable_json(result.structured_content)
if result.structured_content is not None
else None,
"resources": [resource_payload(resource) for resource in result.resources],
```

- [x] 修改 `_select_skill` 的“新选择成功”和“已经选择”两个成功分支，使其都构造 `{"skill_id", "snapshot_id", "version"}`；失败路径保持无资源错误结果。

### 3.3 升级 JSON Store，保持旧会话可读

- [x] 将 `SESSION_SCHEMA_VERSION` 从 8 升到 9。

- [x] v9 消息 JSON 使用字段：

```json
{
  "tool_structured_content": {},
  "tool_resources": [
    {
      "kind": "download",
      "name": "report.md",
      "href": "/downloads/download_123",
      "mime_type": "text/markdown",
      "expires_at": "2026-07-31T00:00:00+00:00"
    }
  ]
}
```

- [x] 对 v1–v8 继续使用既有严格字段集合；加载时补 `tool_structured_content=None`、`tool_resources=()`。

- [x] v9 仍严格拒绝未知字段、错误类型、错误角色上的资源。

### 3.4 实现统一模型投影与稳定规则

- [x] 在 `tool_protocol.py` 增加唯一投影函数：

```python
def model_tool_result_content(message: Message) -> str:
    if message.tool_structured_content is None:
        return message.content
    return json.dumps(
        {
            "content": message.content,
            "structured_content": mutable_json(message.tool_structured_content),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
```

禁止读取 `message.tool_resources`。

- [x] 增加共享规则生成函数，语义固定为：
  - Tool 结果中的 `content` 用于解释；
  - `structured_content` 是唯一机器可读字段；
  - Host 会渲染操作资源；
  - 模型不得构造、猜测或改写下载链接；
  - 不得把内部 ID 当作 URL。

- [x] `json_protocol_prompt()` 为每个 Tool 输出精简的 `output_schema` 与 `result_usage`。

- [x] 将 `native_messages(messages)` 改为 `native_messages(messages, definitions)`，注入共享规则，并对 TOOL 消息调用 `model_tool_result_content()`。

- [x] DeepSeek JSON 和 Native 路径都传入 `request.tools`。

- [x] DeepSeek Native 的官方 token counter 和近似 token counter 也必须向
  `native_messages()` 传入 `request.tools`，使预算计算包含同一稳定规则和简短
  `result_usage`，与真实 Provider 请求保持一致。移除 `native_messages()` 的默认
  definitions 参数，避免后续调用静默遗漏这段上下文。

- [x] Anthropic `_messages()` 接收 definitions，将同一共享规则并入 system 文本；`tool_result` block 只使用 `model_tool_result_content()`。

- [x] 不把完整输出 Schema塞进 Provider 原生 tool description；Native 只使用共享规则和每个 Tool 的简短 `result_usage`，控制 token 成本。

### 3.5 验证 Task 3

- [x] 运行：

```powershell
python -m pytest tests/unit/test_tool_messages.py tests/unit/test_agent_loop.py tests/unit/test_json_store.py tests/contract/test_json_store_contract.py tests/unit/test_tool_protocol.py tests/unit/test_deepseek_llm.py tests/unit/test_provider_adapters.py tests/unit/test_deepseek_token_counter.py tests/unit/test_token_counter.py -q
python -m ruff check src/sjtuclaw/models/message.py src/sjtuclaw/runtime/agent_loop.py src/sjtuclaw/infrastructure/persistence/json_store.py src/sjtuclaw/infrastructure/llm src/sjtuclaw/infrastructure/tokenizers tests/unit/test_tool_messages.py tests/unit/test_agent_loop.py tests/unit/test_json_store.py tests/contract/test_json_store_contract.py tests/unit/test_tool_protocol.py tests/unit/test_deepseek_llm.py tests/unit/test_provider_adapters.py tests/unit/test_deepseek_token_counter.py tests/unit/test_token_counter.py
python -m mypy src/sjtuclaw/models/message.py src/sjtuclaw/runtime/agent_loop.py src/sjtuclaw/infrastructure/persistence/json_store.py src/sjtuclaw/infrastructure/llm src/sjtuclaw/infrastructure/tokenizers tests/unit/test_tool_messages.py tests/unit/test_agent_loop.py tests/unit/test_json_store.py tests/contract/test_json_store_contract.py tests/unit/test_tool_protocol.py tests/unit/test_deepseek_llm.py tests/unit/test_provider_adapters.py tests/unit/test_deepseek_token_counter.py tests/unit/test_token_counter.py
```

预期：全部通过。

- [x] 提交：

```powershell
git commit -m "feat: persist and project typed tool results"
```

---

## Task 4: Gateway 透传资源，Web Host 通用渲染

**Files:**

- Modify: `src/sjtuclaw/entrypoints/gateway.py`
- Modify: `src/sjtuclaw/entrypoints/gateway_streams.py`
- Modify: `tests/integration/test_gateway_runtime.py`
- Modify: `web/src/App.tsx`
- Add: `web/src/toolResources.ts`
- Add: `web/src/toolResources.test.ts`
- Modify: `project_structure.md`

### 4.1 先写 Gateway 和前端失败测试

- [x] 在 `tests/integration/test_gateway_runtime.py` 增加历史消息与 SSE 事件断言：

```json
{
  "structuredContent": {
    "filename": "report.md",
    "size_bytes": 12
  },
  "resources": [
    {
      "kind": "download",
      "name": "report.md",
      "href": "/downloads/download_123",
      "mimeType": "text/markdown",
      "expiresAt": "2026-07-31T00:00:00+00:00"
    }
  ]
}
```

- [x] 新建 `web/src/toolResources.test.ts`，测试 `normalizeToolResources()`：
  - 接受合法 `download` 相对路径；
  - 拒绝 `https://...`、`//host/...`、非 `/downloads/` 路径；
  - 忽略未知 kind；
  - 非数组输入返回空数组。

- [x] 运行：

```powershell
python -m pytest tests/integration/test_gateway_runtime.py -q
Set-Location web
npm test -- --run src/toolResources.test.ts
Set-Location ..
```

预期：Gateway 缺字段，前端模块不存在，测试失败。

### 4.2 Gateway 使用统一 camelCase 资源格式

- [x] 在 `gateway.py` 提取或复用一个资源序列化 helper，`_message_payload()` 增加：

```python
"structuredContent": (
    mutable_json(message.tool_structured_content)
    if message.tool_structured_content is not None
    else None
),
"resources": [_resource_payload(resource) for resource in message.tool_resources],
```

- [x] `_resource_payload()` 输出 `kind`、`name`、`href`、`mimeType`、`expiresAt`。

- [x] `gateway_streams.py` 的 `TOOL_RESULT` 映射输出同样字段名和结构，不自行拼接下载 URL。

### 4.3 前端删除 Tool 特判，改为资源驱动

- [x] 新建 `web/src/toolResources.ts`：

```ts
export type ToolResource = {
  kind: "download";
  name: string;
  href: string;
  mimeType: string | null;
  expiresAt: string | null;
};

const DOWNLOAD_HREF = /^\/downloads\/[A-Za-z0-9_-]{1,256}$/;

export function normalizeToolResources(value: unknown): ToolResource[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isRecord(item) || item.kind !== "download") return [];
    if (typeof item.name !== "string" || !item.name) return [];
    if (typeof item.href !== "string" || !DOWNLOAD_HREF.test(item.href)) return [];
    return [{
      kind: "download",
      name: item.name,
      href: item.href,
      mimeType: typeof item.mimeType === "string" ? item.mimeType : null,
      expiresAt: typeof item.expiresAt === "string" ? item.expiresAt : null,
    }];
  });
}
```

同文件实现本地 `isRecord()`，不新增依赖。

- [x] `App.tsx` 的历史 `Message` 和 `LiveToolItem` 类型增加 `structuredContent`、`resources`。

- [x] SSE `tool_result` 读取 `structuredContent` 和 `resources`，使用 `normalizeToolResources()`。

- [x] `ToolAccordion` 将 `downloadUrl` prop 改为 `resources`，遍历资源渲染：

```tsx
{resources.map((resource) => (
  <a key={`${resource.kind}:${resource.href}`} href={resource.href} download>
    下载 {resource.name}
  </a>
))}
```

- [x] 删除 `downloadUrlFor()` 和 `downloadUrlForTool()`，不再解析 `create_download` 的 `content`，不再从 `download_id` 拼 URL。

- [x] 保留 `App.tsx` 里用户已有的文本/PDF 附件 UI 修改。

- [x] 更新 `project_structure.md`：
  - Web 目录树加入 `toolResources.ts` 与 `toolResources.test.ts`；
  - 模块说明写明“验证并规范化 Host 可渲染的 Tool 资源”；
  - 不改动其他目录说明。

### 4.4 验证 Task 4

- [x] 运行：

```powershell
python -m pytest tests/integration/test_gateway_runtime.py -q
Set-Location web
npm test -- --run
npm run build
Set-Location ..
```

预期：全部通过；TypeScript 不存在隐式 `any` 或无效 prop。

- [x] 选择性暂存，确认未包含 `App.tsx` 的用户原有附件改动，再提交：

```powershell
git diff --cached
git commit -m "feat: render tool resources in the host"
```

---

## Task 5: 端到端防泄漏回归与全量验证

**Files:**

- Modify: `tests/unit/test_agent_loop.py`
- Modify: `tests/unit/test_bootstrap_tools.py`
- Modify: `tests/integration/test_gateway_security.py`
- Modify: `tests/unit/test_downloads.py`

### 5.1 先写端到端回归测试

- [x] 在 `tests/unit/test_agent_loop.py` 增加两轮 LLM 测试：
  1. 第一轮模型调用返回一个 Tool call；
  2. Tool 返回含 `href="/downloads/download_123"` 的资源；
  3. 第二轮捕获传给 LLM 的消息；
  4. 断言模型可见 `content` 和 `structured_content`；
  5. 断言序列化请求中不存在 `resources`、`href`、`download_123`。

- [x] 在 `tests/unit/test_bootstrap_tools.py` 覆盖真实 `create_download`：
  - Runtime `TOOL_RESULT` 事件含资源；
  - 持久化 TOOL 消息含相同资源；
  - 模型下一轮输入不含下载 ID。

- [x] 在 `tests/integration/test_gateway_security.py` 验证：
  - 合法 Host 相对链接仍能下载快照；
  - 过期或未知 ID 保持既有错误行为；
  - Gateway 不接受外部 URL 作为 Tool 下载资源。

- [x] 运行新增测试。如果 Task 1–4 已完整落实，新增断言应直接通过；若失败，只修改失败链路对应的 `agent_loop.py` 或 `tool_protocol.py`，不扩大范围。

### 5.2 运行分层验证

- [x] 后端目标测试：

```powershell
python -m pytest tests/unit/test_tool_models.py tests/unit/test_tool_registry.py tests/unit/test_tool_protocol.py tests/unit/test_tool_messages.py tests/unit/test_json_store.py tests/unit/test_agent_loop.py tests/unit/test_bootstrap_tools.py tests/unit/test_tool_readonly.py tests/unit/test_read_attachment_tool.py tests/unit/test_advanced_tools.py tests/unit/test_read_skill_resource_tool.py tests/unit/test_skill_routing.py tests/unit/test_deepseek_llm.py tests/unit/test_provider_adapters.py tests/unit/test_downloads.py tests/contract/test_json_store_contract.py tests/integration/test_gateway_runtime.py tests/integration/test_gateway_security.py -q
```

- [x] 全量静态检查：

```powershell
python -m ruff check .
python -m mypy
```

- [x] 全量后端测试：

```powershell
python -m pytest
```

- [x] 全量前端验证：

```powershell
Set-Location web
npm test -- --run
npm run build
Set-Location ..
```

- [x] 手工冒烟：
  1. 启动 Gateway；
  2. 让 Agent 对工作区文件执行 `create_download`；
  3. 确认会话卡片显示下载操作；
  4. 确认链接形如 `/downloads/download_<id>`；
  5. 点击后文件正常下载；
  6. 检查 LLM 调试请求，确认没有资源链接和下载 ID；
  7. 刷新页面，确认历史消息仍能显示同一资源操作。

### 5.3 对照验收标准复核

- [x] 全部 11 个内置 Tool 均有强制 `output_schema` 和 `result_usage`。
- [x] Registry 对无效结构化结果统一返回 `TOOL_OUTPUT_SCHEMA_INVALID`。
- [x] `create_download` 的模型视图不含 `download_id`、`href`。
- [x] Host 只使用 Tool 返回的相对 `href`，不再解析 Tool 文本或拼接 ID。
- [x] v1–v8 会话仍能加载；v9 能往返结构化结果和资源。
- [x] DeepSeek Native、DeepSeek JSON、Anthropic 三条路径均不泄漏资源。
- [x] 没有回退用户当前的工作区改动。

### 5.4 最终提交与汇报

- [x] 检查暂存内容：

```powershell
git status --short
git diff --cached --stat
git diff --cached
```

- [x] 若选择性暂存安全，提交最后的回归测试：

```powershell
git commit -m "test: prevent tool resource leakage to models"
```

- [x] 汇报：
  - 具体改动；
  - 实际运行的验证命令及结果；
  - 未运行的 live Provider 测试；
  - 旧会话兼容边界；
  - 当前仍未提交的用户原有改动。
