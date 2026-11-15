# 统一 CLI 与 Web 命令系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 CLI 与 Web 共用除 `/exit` 外的命令目录、执行逻辑和 `/command` 帮助，并在 Web Composer 提供补全与临时结果卡片。

**Architecture:** 新建 runtime 命令服务，解析命令并产出与入口无关的 `CommandOutcome`。CLI 负责将 Outcome 打印到终端；Gateway 负责鉴权、序列化 Outcome，并在显式 Skill 调用时提交正常 Run；Web 负责补全、结果卡片和刷新/选中效果。

**Tech Stack:** Python 3.12、FastAPI、React 19、TypeScript、Vite、Vitest、pytest、Ruff、mypy。

## Global Constraints

- `/exit` 仅保留在 CLI；所有其他现有 `/session`、`/memory`、`/compact` 和 `/skill` 命令两端可用。
- `presentation-outline` 仅示例；Skill 名称必须从当前 `SkillService` 清单动态解析，不得硬编码。
- `/skill show <skill-name>` 为公开查看命令；`/skill <skill-name> <task>` 立即提交带固定 Skill reference 的正常 Run。
- 命令结果不得写入 Session 消息、Run 历史或长期记忆；Web 刷新后可以消失。
- Gateway 命令端点必须保留既有 Cookie 鉴权、同源校验、输入上限和 loopback 边界；不得引入任意 shell 执行或扩大 Tool 权限。
- 新增 runtime 源文件时同步更新 `project_structure.md`。

---

## 文件结构

- Create: `src/sjtuclaw/runtime/commands.py` — 命令目录、解析、服务协议和入口无关 Outcome。
- Modify: `src/sjtuclaw/runtime/__init__.py` — 导出公共命令类型与服务。
- Modify: `src/sjtuclaw/bootstrap.py` — 组装一次 `CommandService` 并放入 `RuntimeApplication`。
- Modify: `src/sjtuclaw/entrypoints/cli.py` — 将 CLI 命令分发替换为共享服务，保留 CLI 专属 `/exit`。
- Modify: `src/sjtuclaw/entrypoints/gateway.py` — 暴露命令目录和执行端点，并提交显式 Skill Run。
- Create: `tests/unit/test_commands.py` — 覆盖目录、命令分支、错误和 Submission Outcome。
- Modify: `tests/unit/test_cli_commands.py` — 覆盖 `/command`、`/skill show` 和共享分发。
- Modify: `tests/integration/test_gateway_runtime.py` — 覆盖 Gateway 命令端点、鉴权、同源和 Run。
- Create: `web/src/commands.ts` — Web 命令 DTO、补全筛选、选择移动和模板应用纯函数。
- Create: `web/src/commands.test.ts` — Vitest 覆盖固定与动态 Skill 补全、键盘选择和模板填充。
- Modify: `web/src/App.tsx` — 获取目录、执行命令、渲染卡片、驱动 Composer 补全。
- Modify: `web/src/styles.css` — 补全弹层与结果卡片样式及小屏适配。
- Modify: `web/package.json`、`web/package-lock.json` — 添加 Vitest 和 `npm run test`。
- Modify: `README.md`、`project_structure.md` — 记录 `/command`、完整命令语法和 runtime 模块职责。

## Task 1: Runtime 命令目录与执行服务

**Files:**
- Create: `src/sjtuclaw/runtime/commands.py`
- Modify: `src/sjtuclaw/runtime/__init__.py`
- Test: `tests/unit/test_commands.py`

**Interfaces:**
- Consumes: `SessionService`, `MemoryService`, `CompactionService`, `SkillService`, `CompactionResult`, `SkillReference`。
- Produces: `CommandCatalog.descriptors() -> tuple[CommandDescriptor, ...]`；`CommandService.execute(raw: str, *, current_session_id: str) -> CommandOutcome | None`。

- [ ] **Step 1: Write failing catalog and parser tests**

```python
async def _assert_catalog_and_parser() -> None:
    service = _command_service()
    outcome = await service.execute("/command", current_session_id="session_001")
    assert outcome is not None
    assert outcome.is_error is False
    assert "/skill show <skill-name>" in outcome.lines
    assert "/exit" not in "\n".join(outcome.lines)

    unknown = await service.execute("/not-a-command", current_session_id="session_001")
    assert unknown is not None
    assert unknown.is_error is True
    assert unknown.lines == ("Error> unknown or incomplete command: /not-a-command",)

    assert await service.execute("ordinary message", current_session_id="session_001") is None
```

- [ ] **Step 2: Run failing catalog and parser tests**

Run: `py -3.12 -m pytest tests/unit/test_commands.py -q -p no:cacheprovider`

Expected: FAIL because `sjtuclaw.runtime.commands` does not exist.

- [ ] **Step 3: Implement command data contracts and catalog**

```python
@dataclass(frozen=True, slots=True)
class CommandDescriptor:
    template: str
    description: str
    skill_argument: bool = False

@dataclass(frozen=True, slots=True)
class CommandSubmission:
    text: str
    skill_refs: tuple[SkillReference, ...]

@dataclass(frozen=True, slots=True)
class CommandEffects:
    refresh_sessions: bool = False
    refresh_memories: bool = False
    refresh_skills: bool = False
    select_session_id: str | None = None

@dataclass(frozen=True, slots=True)
class CommandOutcome:
    raw: str
    lines: tuple[str, ...]
    is_error: bool = False
    effects: CommandEffects = field(default_factory=CommandEffects)
    submission: CommandSubmission | None = None

class CommandCatalog:
    def descriptors(self) -> tuple[CommandDescriptor, ...]: ...

class CommandService:
    async def execute(
        self, raw: str, *, current_session_id: str
    ) -> CommandOutcome | None: ...
```

Use `str.split(maxsplit=...)` so titles and task text retain spaces. The catalog must publish canonical `/skill show <skill-name>`; do not publish `/exit`.

- [ ] **Step 4: Add failing behavior tests for all command families**

```python
async def _assert_command_effects() -> None:
    service = _command_service()
    created = await service.execute("/session new", current_session_id="session_001")
    assert created is not None
    assert created.effects.refresh_sessions is True
    assert created.effects.select_session_id == "session_002"

    memory = await service.execute("/memory add exam is Friday", current_session_id="session_001")
    assert memory is not None
    assert memory.effects.refresh_memories is True

    shown = await service.execute("/skill show course-report", current_session_id="session_001")
    assert shown is not None
    assert shown.lines == ("Skill> $course-report v1.0.0", "write a course report")

    submitted = await service.execute(
        "/skill course-report summarize lecture", current_session_id="session_001"
    )
    assert submitted is not None
    assert submitted.submission is not None
    assert submitted.submission.text == "summarize lecture"
    assert submitted.submission.skill_refs[0].skill_id == "course-report"
```

Also add isolated tests for `/session list|switch|rename|delete`, `/memory list|delete`, `/compact`, `/skill list|usage|refresh`, unavailable services, missing Skill, missing Memory, empty rename title and missing required arguments. Assert errors are `CommandOutcome(is_error=True)` rather than uncaught exceptions.

- [ ] **Step 5: Implement all command branches with minimal service calls**

```python
if raw == "/compact":
    if self._compaction is None:
        return _error(raw, "Compaction> service is unavailable")
    result = await self._compaction.compact_session(current_session_id, force=True)
    return _compaction_outcome(raw, result)

if len(parts) == 3 and parts[:1] == ["/skill"]:
    references = self._require_skills(raw).resolve_current_names((parts[1],))
    return CommandOutcome(
        raw,
        (f"Skill> selected ${parts[1]} for this request",),
        submission=CommandSubmission(parts[2], references),
    )
```

For deletion of the selected Session, use the same fallback behavior as CLI today: choose the first remaining active Session, or create one. Convert `StoreError`, `SkillError`, `ValueError` and expected compaction failure states into stable Outcome lines without leaking local paths.

- [ ] **Step 6: Run runtime command tests and static checks**

Run: `py -3.12 -m pytest tests/unit/test_commands.py -q -p no:cacheprovider; py -3.12 -m ruff check src/sjtuclaw/runtime/commands.py tests/unit/test_commands.py --no-cache; py -3.12 -m mypy src/sjtuclaw/runtime/commands.py tests/unit/test_commands.py --no-incremental`

Expected: all command tests pass; Ruff and mypy report no issue.

- [ ] **Step 7: Commit runtime command service**

```powershell
git add src/sjtuclaw/runtime/commands.py src/sjtuclaw/runtime/__init__.py tests/unit/test_commands.py
git commit -m "feat: add shared command service"
```

## Task 2: CLI 使用共享命令服务

**Files:**
- Modify: `src/sjtuclaw/bootstrap.py`
- Modify: `src/sjtuclaw/entrypoints/cli.py`
- Modify: `tests/unit/test_cli_commands.py`
- Modify: `tests/unit/test_bootstrap_tools.py`

**Interfaces:**
- Consumes: `CommandService`, `CommandOutcome`, `CommandSubmission` from Task 1.
- Produces: `RuntimeApplication.commands: CommandService`; CLI prints Outcome lines and submits `outcome.submission` in the same iteration.

- [ ] **Step 1: Write failing CLI tests for canonical help and Skill show**

```python
async def _assert_shared_cli_commands() -> None:
    output: list[str] = []
    handled = await _handle_command("/command", _application(), CLIState("session_001"), output.append)
    assert handled is True
    assert "/memory add <content>" in output

    output.clear()
    handled = await _handle_command(
        "/skill show course-report", _application(), CLIState("session_001"), output.append
    )
    assert handled is True
    assert output == ["Skill> $course-report v1.0.0", "write a course report"]
```

- [ ] **Step 2: Run failing CLI tests**

Run: `py -3.12 -m pytest tests/unit/test_cli_commands.py -q -p no:cacheprovider`

Expected: FAIL because the existing `_handle_command` has no shared `CommandService` path and interprets `show` as a Skill ID.

- [ ] **Step 3: Wire `CommandService` during bootstrap and replace CLI branching**

```python
commands = CommandService(
    sessions=session_service,
    memories=memory_service,
    compaction=compaction,
    skills=skills,
)

return RuntimeApplication(..., commands=commands, ...)
```

Keep `/exit` in `run_cli` before command dispatch. Make `_handle_command` a thin adapter: call `application.commands.execute`, print `outcome.lines`, update `CLIState.session_id` from `outcome.effects.select_session_id`, and copy `outcome.submission` into the immediate RuntimeRequest. Remove duplicated parser branches only after every prior command behavior is represented by `CommandService`.

- [ ] **Step 4: Run CLI and bootstrap regression tests**

Run: `py -3.12 -m pytest tests/unit/test_cli_commands.py tests/unit/test_bootstrap_tools.py -q -p no:cacheprovider`

Expected: `/exit` remains CLI-only; `/command`, `/skill show`, existing Session/Memory/Compact commands and explicit Skill submission pass.

- [ ] **Step 5: Commit CLI integration**

```powershell
git add src/sjtuclaw/bootstrap.py src/sjtuclaw/entrypoints/cli.py tests/unit/test_cli_commands.py tests/unit/test_bootstrap_tools.py
git commit -m "feat: share commands with CLI"
```

## Task 3: Gateway 命令目录与执行端点

**Files:**
- Modify: `src/sjtuclaw/entrypoints/gateway.py`
- Modify: `tests/integration/test_gateway_runtime.py`
- Modify: `tests/unit/test_gateway_startup.py`

**Interfaces:**
- Consumes: `CommandService`, `CommandDescriptor`, `CommandOutcome`, `CommandSubmission` and `RuntimeApplication.commands`.
- Produces: `GET /api/v1/commands` and `POST /api/v1/sessions/{session_id}/commands`.

- [ ] **Step 1: Write failing Gateway contract tests**

```python
def test_gateway_lists_and_executes_commands() -> None:
    client, options, session, runtime = _command_gateway()
    _bootstrap(client, options)

    catalog = client.get("/api/v1/commands")
    listed = client.post(
        f"/api/v1/sessions/{session.session_id}/commands",
        headers={"Origin": options.origin},
        json={"command": "/skill list", "idempotencyKey": "command-001"},
    )

    assert catalog.status_code == 200
    assert "/skill show <skill-name>" in [item["template"] for item in catalog.json()["commands"]]
    assert listed.status_code == 200
    assert listed.json()["outcome"]["isError"] is False
```

Add request cases proving unauthenticated calls return 401, cross-origin command posts fail, missing Session returns 404, archived Session is rejected, unknown command returns a 200 structured error Outcome, and `/exit` is absent from the catalog.

- [ ] **Step 2: Run failing Gateway contract tests**

Run: `py -3.12 -m pytest tests/integration/test_gateway_runtime.py -q -p no:cacheprovider`

Expected: FAIL because neither command route exists.

- [ ] **Step 3: Add DTOs, dependency and catalog route**

```python
class ExecuteCommandBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    command: str = Field(min_length=1, max_length=100_000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200, alias="idempotencyKey")

@app.get(f"{API_PREFIX}/commands")
async def list_commands(...):
    return {"commands": [_command_descriptor_payload(item) for item in service.descriptors()]}
```

Add `commands: CommandService | None` to `create_gateway`, `require_commands`, `RuntimeApplication`, and `run_gateway`. Serialize `CommandOutcome` with `lines`, `isError`, `effects`, and no internal exception data.

- [ ] **Step 4: Implement authenticated command execution and explicit Skill Run submission**

```python
outcome = await service.execute(body.command, current_session_id=session_id)
payload: dict[str, object] = {"outcome": _command_outcome_payload(outcome)}
if outcome.submission is not None:
    runtime_request = RuntimeRequest(
        request_id=uuid4().hex,
        session_id=session_id,
        source=InvocationSource.GATEWAY,
        user_input=UserInput(outcome.submission.text),
        idempotency_key=body.idempotency_key,
        skill_refs=outcome.submission.skill_refs,
    )
    await active_runtime.submit(runtime_request)
    payload["request"] = _run_payload(_require_submitted_run(...))
return payload
```

Call `require_same_origin` before execution. Validate active mutable Session before invocation. Do not route command text to the normal message endpoint or expose a generic callable name.

- [ ] **Step 5: Run Gateway regression tests and commit**

Run: `py -3.12 -m pytest tests/integration/test_gateway_runtime.py tests/unit/test_gateway_startup.py -q -p no:cacheprovider`

Expected: all existing Gateway runtime tests plus command contracts pass.

```powershell
git add src/sjtuclaw/entrypoints/gateway.py tests/integration/test_gateway_runtime.py tests/unit/test_gateway_startup.py
git commit -m "feat: expose command gateway API"
```

## Task 4: Web 命令补全、执行与结果卡片

**Files:**
- Create: `web/src/commands.ts`
- Create: `web/src/commands.test.ts`
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`
- Modify: `web/package.json`
- Modify: `web/package-lock.json`

**Interfaces:**
- Consumes: Gateway `/commands` DTO, existing `SkillManifest`, command execution response and existing `Run`/SSE APIs.
- Produces: `commandSuggestions`, `moveSuggestion`, `applySuggestion`; per-Session transient `CommandCard` rendering; command POST client path.

- [ ] **Step 1: Add a failing pure Web command test suite**

```ts
import { describe, expect, it } from "vitest";
import { applySuggestion, commandSuggestions, moveSuggestion } from "./commands";

describe("commandSuggestions", () => {
  it("expands dynamic Skill IDs without hard-coding presentation names", () => {
    const definitions = [{ template: "/skill show <skill-name>", description: "show Skill", skillArgument: true }];
    const items = commandSuggestions(definitions, [
      { skillId: "course-report", available: true },
      { skillId: "presentation-outline", available: true },
    ], "/skill show c");
    expect(items.map((item) => item.value)).toEqual(["/skill show course-report"]);
  });

  it("wraps keyboard selection and fills a template", () => {
    expect(moveSuggestion(0, -1, 3)).toBe(2);
    expect(applySuggestion("/mem", "/memory add ")).toBe("/memory add ");
  });
});
```

- [ ] **Step 2: Install test runner and verify red state**

Run: `npm install --save-dev vitest; npm run test -- --run web/src/commands.test.ts`

Expected: initial test run fails because `web/src/commands.ts` and `test` script are absent.

- [ ] **Step 3: Implement Web command DTO and pure autocomplete helpers**

```ts
export type CommandDefinition = { template: string; description: string; skillArgument: boolean };
export type CommandSuggestion = { value: string; description: string };
export type CommandSkill = { skillId: string; available: boolean };

export function commandSuggestions(
  definitions: CommandDefinition[], skills: CommandSkill[], input: string,
): CommandSuggestion[] { ... }

export function moveSuggestion(index: number, delta: number, count: number): number { ... }
export function applySuggestion(_input: string, value: string): string { return value; }
```

Filter only when trimmed input starts with `/`. Expand `skillArgument` candidates from available `skillId` values, preserving their actual IDs. Return no suggestions for ordinary chat text.

- [ ] **Step 4: Wire Composer state, keyboard behavior and command execution**

```tsx
const [commandDefinitions, setCommandDefinitions] = useState<CommandDefinition[]>([]);
const [commandCards, setCommandCards] = useState<Record<string, CommandCard[]>>({});
const [commandIndex, setCommandIndex] = useState(0);

if (cleanText.startsWith("/")) {
  await executeCommand(sessionId, cleanText);
  return;
}
```

Fetch `/commands` with initial Skill refresh. Render a `role="listbox"` below Composer only while suggestions exist; use `aria-activedescendant`, `role="option"`, `ArrowUp`, `ArrowDown`, `Tab`, `Enter`, and `Escape`. Ignore all selection and submit shortcuts while `event.nativeEvent.isComposing` is true. On command response append a success/error card under the selected Session's chat history, clear Composer text, apply `refreshSessions`, `refreshMemories`, `refreshSkills`, and `selectSessionId`, and subscribe to `response.request.runId` when an explicit Skill command starts a Run.

- [ ] **Step 5: Add focused styles and run Web tests/build**

Add `.command-completions`, `.command-completion--active`, `.command-card`, `.command-card--error`, and responsive constraints matching existing Composer tokens. Do not change unrelated panels.

Run: `npm run test -- --run; npm run build`

Expected: Vitest passes, TypeScript checks the new DTOs, and Vite produces a production bundle.

- [ ] **Step 6: Perform browser acceptance checks and commit**

Check manually in the local Gateway UI:

1. Type `/` and confirm `/command` is suggested.
2. Type `/skill show ` and confirm available Skill IDs appear, including an ID that is not `presentation-outline`.
3. Use arrows, Tab, Enter, Escape and Chinese IME; only the intended action occurs.
4. Execute `/command`, `/skill list`, `/memory list`, and an invalid command; cards are visible and no command text becomes a user message.
5. Execute `/skill <skill-name> <task>`; a normal Run starts and Skill usage appears after it completes.

```powershell
git add web/src/commands.ts web/src/commands.test.ts web/src/App.tsx web/src/styles.css web/package.json web/package-lock.json
git commit -m "feat: add web command palette"
```

## Task 5: 文档、结构索引与完整验证

**Files:**
- Modify: `README.md`
- Modify: `project_structure.md`

**Interfaces:**
- Consumes: final catalog templates from `CommandCatalog`.
- Produces: user-facing command reference and accurate source-module structure map.

- [ ] **Step 1: Document canonical command usage**

Add one README command section containing exactly:

```text
/command
/skill list
/skill show <skill-name>
/skill <skill-name> <task>
/skill usage
```

State that Skill names come from the dynamic list and that Web command cards are temporary. Update `project_structure.md` to describe `runtime/commands.py` as shared command catalog and executor.

- [ ] **Step 2: Run complete verification from final working tree**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
py -3.12 -m pytest -m "not live" -q -p no:cacheprovider
py -3.12 -m ruff check . --no-cache
py -3.12 -m mypy src --no-incremental
Set-Location web; npm run test -- --run; npm run build
Set-Location ..; git diff --check
```

Expected: all non-live Python tests pass, Ruff and mypy report no issue, Vitest passes, frontend build succeeds, and Git reports no whitespace error.

- [ ] **Step 3: Commit documentation and verification-ready changes**

```powershell
git add README.md project_structure.md
git commit -m "docs: document unified commands"
git status --short
```

Expected: working tree is clean before final handoff.
