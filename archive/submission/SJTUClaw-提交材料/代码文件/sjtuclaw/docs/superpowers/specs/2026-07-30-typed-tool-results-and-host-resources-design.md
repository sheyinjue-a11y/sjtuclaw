# 类型化 Tool 结果与 Host 资源渲染设计

**日期：** 2026-07-30

**状态：** 已确认，可进入实施计划阶段

## 背景

SJTUClaw 当前只校验 Tool 输入，没有声明和校验 Tool 输出。各 Handler
统一返回字符串，但有些字符串是普通文本，有些字符串包含 JSON。同一个字符串会被：

- 持久化为 Tool `Message`；
- 重新发送给模型；
- 通过 Gateway 暴露给前端；
- 由网页客户端按需解析。

这种设计存在“权威来源”缺口。以 `create_download` 为例：

1. Tool 正确创建本地下载快照并返回不透明 ID。
2. 网页客户端可以根据 ID 构造正确的本地路由。
3. 模型仍可能在最终回答中把 ID 和文件名拼成一个看似合理的外部 URL。
4. 用户点击模型编造的 S3 链接后，访问了一个实际不存在的 Bucket。

虽然下载快照本身创建成功，但模型生成的链接不是 Tool 结果，因而可能完全错误。

本设计需要明确 Tool 结果结构，并把面向用户的资源入口从模型生成文本中移交给
Host 确定性渲染。

## 目标

1. 要求每个注册 Tool 必须声明输出 JSON Schema。
2. 每个成功的 Tool 输出必须先通过校验，才能被持久化或显示为成功。
3. 给模型提供边界明确、使用方式清楚的 Tool 结果视图。
4. 下载等可操作资源必须由 Host 控制，不能依赖模型拼接链接。
5. 网页客户端根据已经校验的资源数据确定性渲染操作入口。
6. 保持已有 Session 文件可读，不执行批量迁移。
7. 保留现有 AgentLoop、权限、审批和 Provider 架构。

## 本次不包含的工作

- 将 SJTUClaw 改造成 MCP Server。
- 完整引入 MCP 协议。
- 增加 S3 或其他远程 Artifact 存储。
- 增加 `SJTUCLAW_PUBLIC_BASE_URL`。
- 追踪 Assistant 普通 Markdown 中每一个任意链接的来源。
- 批量改写已有 Session 文件。
- 重构与 Tool 结果契约无关的 Gateway、前端或运行时逻辑。

## 选定方案

采用一个精简的、受 MCP 启发的内部结果契约：

```python
@dataclass(frozen=True, slots=True)
class ToolExecutionOutput:
    content: str
    structured_content: Mapping[str, JsonValue]
    resources: tuple[ToolResourceLink, ...] = ()
```

三个字段职责如下：

- `content`：提供给模型阅读的正文或简短说明。
- `structured_content`：经过 Schema 校验、供推理、审计和 UI 读取的结构化数据。
- `resources`：由 Host 管理和渲染的操作或链接，不发送给模型。

本方案不会完整复制 MCP。SJTUClaw 继续使用现有 Tool 调用、权限、Session
和 Provider Adapter。

## ToolDefinition

`ToolDefinition` 增加两个必填字段和一个资源类型声明：

```python
@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, JsonValue]
    output_schema: Mapping[str, JsonValue]
    result_usage: str
    safety_level: ToolSafetyLevel
    allowed_resource_kinds: tuple[str, ...] = ()
```

字段语义：

- `output_schema` 描述 `ToolExecutionOutput.structured_content`。
- `result_usage` 简短说明模型可以如何使用结果，以及禁止从结果推导什么。
- `allowed_resource_kinds` 默认是空元组。Tool 不能返回未声明的资源类型。

Schema 规则：

- 输入和输出 Schema 使用 JSON Schema 2020-12。
- 内置 Tool 的输出 Schema 根节点统一为对象。
- 明确列出所有必填字段。
- 使用 `additionalProperties: false` 禁止未声明字段。
- 可空值必须显式包含 `null` 类型。
- 字段描述必须说明不透明 ID、文件系统路径、游标、URL、截断状态等容易误用的值。

ToolRegistry 构造时必须拒绝缺失或非法的输出 Schema。

## 资源契约

第一阶段只支持 `download` 资源：

```python
@dataclass(frozen=True, slots=True)
class ToolResourceLink:
    kind: str
    name: str
    href: str
    mime_type: str | None = None
    expires_at: str | None = None
```

资源由统一逻辑校验：

- `kind` 必须存在于 Tool 的 `allowed_resource_kinds` 中。
- `href` 必须是以单个 `/` 开头的同源绝对路径。
- `href` 禁止包含 scheme、authority、控制字符、fragment 或路径穿越。
- `download` 资源必须匹配 `/downloads/<opaque-download-id>`。
- `name` 及可选元数据必须有长度上限。
- 单次 Tool 结果中的资源数量必须有上限。
- 下载文件名只能作为元数据，不能被插入 URL 路径。

`CreateDownloadTool` 产生如下资源：

```json
{
  "kind": "download",
  "name": "responsibility.md",
  "href": "/downloads/download_1e11cd0337e44f5a93cc13c666d647de",
  "mime_type": "text/markdown",
  "expires_at": "2026-07-30T08:00:00+00:00"
}
```

相对路由可以自动适配 Gateway 端口变化，也能在同源反向代理下继续工作。

## 模型视图与 Host 视图

同一个已验证 Tool 结果提供两种投影视图。

### 模型视图

Provider Adapter 只序列化：

```json
{
  "content": "下载快照已创建。界面会显示下载入口。不要提供或构造下载链接。",
  "structured_content": {
    "filename": "responsibility.md",
    "size_bytes": 1234,
    "sha256": "...",
    "expires_at": "2026-07-30T08:00:00+00:00"
  }
}
```

模型不会获得 `download_id` 或 `href`，因此无法根据 Tool 提供的信息拼接下载 URL。

共享的 Tool 使用规则还需要明确：

- ID 是不透明标识，禁止拆解、转换或推导。
- 文件系统路径不是 URL。
- 只有被 Tool 明确标记为“模型可见且权威”的 URL 字段才能原样使用。
- Host 渲染的资源只需要在回答中说明已经创建，模型不得构造链接。
- 结果被截断时，模型不得声称已经获得完整内容。

Native Tool 模式和 JSON Tool 模式必须生成相同的模型视图。

JSON Tool 模式在 Tool 协议说明中提供 `output_schema` 和 `result_usage`。

Native Tool 模式只提供精简的返回字段说明以及共享 Tool 规则，不把每个完整输出
Schema 重复塞进函数描述，避免浪费上下文。

### Host 视图

Gateway 和网页客户端可以获得：

- `content`；
- `structuredContent`；
- `resources`；
- 已有的 Tool 名称、状态、错误码、调用 ID 和时间信息。

Host 不从模型回答中提取或推导可操作 URL。

## 领域模型和持久化

`ToolResult` 增加：

```python
structured_content: Mapping[str, JsonValue] | None = None
resources: tuple[ToolResourceLink, ...] = ()
```

`Message` 增加只允许 Tool Message 使用的对应字段：

```python
tool_structured_content: Mapping[str, JsonValue] | None = None
tool_resources: tuple[ToolResourceLink, ...] = ()
```

规则：

- 新产生的成功 Tool 结果必须包含经过验证的 `structured_content`。
- 错误结果继续使用安全文本和 `error_code`，不能包含资源。
- 非 Tool Message 不能携带 Tool 结果字段。
- `_persist_results` 在现有副作用提交点，把结构化数据和资源从 `ToolResult`
  复制到 Tool `Message`。
- Session JSON Serializer 写入新增可选字段。
- Session JSON Loader 在字段缺失时使用 `None` 和空元组。
- 缺少新字段的旧 Tool Message 视为 Legacy Observation。
- Legacy Observation 保留原始 `content`，不从字符串反推结构化数据或资源。
- 不增加 `contract_version`，也不批量迁移历史文件。

## Registry 执行流程

`ToolHandler.execute` 的返回类型从 `str` 改为 `ToolExecutionOutput`。

每次 Handler 成功返回后，ToolRegistry 按顺序执行：

1. 检查返回值是否为 `ToolExecutionOutput`。
2. 冻结 `structured_content`，并使用对应 `output_schema` 校验。
3. 校验资源数量、字段、路由格式和允许的资源类型。
4. 全部通过后才创建成功的 `ToolResult`。

如果输出验证失败，调用结果变为：

```json
{
  "status": "error",
  "error_code": "TOOL_OUTPUT_SCHEMA_INVALID",
  "content": "tool returned an invalid result"
}
```

具体 Schema 路径和验证错误只记录在服务端日志中，不暴露给模型或用户。

其他 Tool 执行错误继续使用现有的安全错误边界。

## 内置 Tool 输出契约

本次修改必须一次性迁移全部现有内置 Tool。

| Tool | 结构化字段 | 资源类型 | 重要使用规则 |
|---|---|---|---|
| `current_time` | `iso8601` | 无 | 使用包含时区的精确值。 |
| `list_dir` | `path`、`entries[{name,type}]` | 无 | 条目名是文件系统名称，不能据此访问列出目录之外的位置。 |
| `read_file` | `path`、`size_bytes` | 无 | 文件正文位于 `content`；文件过大时调用会失败，因此成功结果是完整内容。 |
| `read_attachment` | `attachment_id`、`truncated`、`next_cursor` | 无 | `truncated=true` 时必须使用 `next_cursor` 继续读取，正文位于 `content`。 |
| `copy_attachment_to_workspace` | `attachment_id`、`destination`、`size_bytes`、`sha256`、`overwritten` | 无 | `destination` 是 Workspace 路径，不是 URL。 |
| `write_file` | `path`、`size_bytes`、`sha256`、`overwritten` | 无 | 成功只表示写入完成，不代表业务内容验证通过。 |
| `create_download` | `filename`、`size_bytes`、`sha256`、`expires_at` | `download` | 入口由界面渲染，模型不得构造链接。 |
| `new_shell` | `cwd`、`workspace_revision`、`replaced_existing` | 无 | `cwd` 是文件系统目录。 |
| `run_command` | `command`、`cwd_before`、`cwd_after`、`exit_code`、`stdout`、`stderr`、`timed_out`、`truncated` | 无 | 必须正确处理非零退出码、超时和输出截断。 |
| `read_skill_resource` | `skill_id`、`path`、`truncated`、`next_cursor` | 无 | 截断时必须继续读取，资源正文位于 `content`。 |
| `select_skill` | `skill_id`、`snapshot_id`、`version` | 无 | ID 是不透明值，只有精确选中的快照可信。 |

大段文本不能在 `content` 和 `structured_content` 中重复保存。

`read_file`、`read_attachment` 和 `read_skill_resource` 把正文放入 `content`，
只把元数据放入结构化对象。

## Gateway 和网页客户端

Gateway 的 Tool Result 事件和 Session Message Payload 增加：

```json
{
  "structuredContent": {},
  "resources": []
}
```

React 的 `Message` 类型和实时 Tool 状态增加对应字段。

`ToolAccordion` 增加小型通用资源操作区：

- `download` 类型使用已验证的 `href` 渲染 `<a>`。
- 可见标签使用资源的 `name`。
- 存在 `expires_at` 时可以显示到期时间。
- 未知资源类型不能渲染成可操作控件。

删除现有 `downloadUrlFor()` 和 `downloadUrlForTool()`。

前端不再解析 `create_download` 的 JSON，也不再根据 `download_id` 构造路由。

Assistant Markdown 仍是普通文本。Tool 资源入口只能来自经过验证的 Host
Resource，因此产品不依赖最终 Assistant Message 暴露下载操作。

## 错误处理

- 输入 Schema 不匹配：继续作为 Tool Call Validation Error。
- Handler 业务失败：继续作为 Tool Execution Error。
- 输出 Schema 或资源校验失败：返回 `TOOL_OUTPUT_SCHEMA_INVALID`。
- 错误结果不能携带可操作资源。
- 前端不能为失败的 Tool 结果渲染资源操作。
- 下载快照缺失或过期：继续使用现有 Gateway 下载错误处理。

## 验证方案

### 领域模型和 Registry

- `ToolDefinition` 拒绝缺失或非法的输出 Schema，以及空的 `result_usage`。
- Registry 接受符合 Schema 的结构化结果。
- Registry 拒绝缺字段、多余字段、错误类型、非法 JSON 值、未声明资源类型、
  不安全 `href` 和资源数量超限。
- 输出校验失败时不能创建成功 `ToolResult`。

### 内置 Tool

- 每个内置 Tool 都声明合法的输出 Schema。
- 每个正常 Handler 结果都符合自身 Schema。
- 文本分页、命令超时和截断、文件 Hash、覆盖标记、下载到期时间保持原语义。
- `create_download` 只产生一个经过验证的相对 `download` 资源。

### Runtime、持久化和 Provider

- AgentLoop 在现有提交点持久化结构化数据和资源。
- 旧 Session Fixture 可以加载，新增字段使用空值。
- 新 Session 可以通过 JSON Store 完整往返。
- Native 和 JSON Provider Message 包含相同的模型视图。
- 模型请求中不包含 Download ID 或 Host-only `href`。
- 错误结果不包含资源。

### Gateway 和前端

- Session 和 SSE Payload 暴露经过验证的资源。
- 资源操作根据 `resources` 渲染。
- 下载按钮使用资源中完全相同的相对 `href`。
- 失败或未知类型资源不能渲染成操作控件。
- 旧 Tool Message 仍能显示原始文本。
- 旧的按 Tool 名称解析下载地址的逻辑已删除。
- 前端测试、TypeScript 检查和生产构建通过。

### 端到端验收

1. 绑定 Workspace 并创建一个文件。
2. 调用 `create_download`。
3. 确认 Tool 成功，界面出现下载操作。
4. 确认 `<a>` 指向当前 Origin 下的 `/downloads/<download-id>`。
5. 下载文件，验证内容和文件名。
6. 检查下一次模型请求，确认其中不包含 Download ID 或 Host-only `href`。
7. 确认最终 Assistant Response 只说明下载已经生成，没有编造链接。

## 风险与控制要求

| 风险 | 可能影响 | 控制要求 |
|---|---|---|
| `Message` 增加 Tool 专用字段 | Session 持久化或 Provider Projection 可能遗漏新字段，也可能破坏旧 Session 加载。 | 为字段缺失、字段存在、JSON 往返和两种 Provider 模式分别增加兼容测试。 |
| Schema 只能验证结构，不能证明业务语义正确 | Handler 可能返回类型正确但含义错误的数据。 | 保留每个 Handler 的业务单元测试，并在 Schema 字段描述中明确单位、路径类型、不透明 ID 和截断语义。 |
| 下载资源使用同源相对路径 | 如果未来把网页和 Gateway 部署到不同 Origin，当前 `href` 将无法直接访问。 | 本次验收固定为同源部署；未来若引入跨域部署，必须另行设计可信公共地址和跨域策略。 |
| Legacy Result 没有结构化数据和可信资源 | 历史 Tool Message 只能展示原始文本，不能自动恢复下载按钮等资源操作。 | 保持 Legacy Result 只读兼容；需要资源时重新调用对应 Tool，不从旧字符串反推链接。 |
