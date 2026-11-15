# Theme and Runtime Provider Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Add persistent light/dark/system themes and secure, immediate switching among DeepSeek, OpenAI-compatible, and Anthropic model APIs.

**Architecture:** A Gateway-owned provider runtime holds the current immutable client/tokenizer record. Every Run leases one record, so a saved replacement affects only later Runs. The React app stores theme only; it sends API Keys once to the authenticated local Gateway.

**Tech Stack:** Python 3.12, FastAPI, httpx, Windows DPAPI through ctypes, React 19, TypeScript, CSS, pytest, Vite.

## Execution status

Implemented on 2026-07-25. The implementation keeps API Keys out of browser
storage and responses, uses current-user DPAPI persistence on Windows, switches
only future runs, and uses DeepSeek's exact tokenizer with approximation for
the other protocols. Verification: `266 passed`, `ruff`, `mypy`, Vite build,
Windows DPAPI round trip, and local visual checks for light/dark settings.

## Global Constraints

- API Keys never enter localStorage, Session JSON, SSE payloads, browser logs, Gateway responses, or error messages.
- Windows persistence uses current-user DPAPI with no third-party dependency; non-Windows persistence is process-only.
- New settings affect the next unstarted Run; active Runs, compaction, and title generation retain their original lease.
- DeepSeek uses DeepSeekTokenCounter; OpenAI-compatible and Anthropic use ApproximateTokenCounter.
- Supported protocols are OpenAI Chat Completions and Anthropic Messages only.
- Preserve loopback/Cookie authorization, streaming, tool, approval, Session, and attachment behavior.
- Do not introduce a frontend test framework or refactor unrelated modules.

---

## File Structure

| File | Responsibility |
| --- | --- |
| \`src/sjtuclaw/config.py\` | Provider enum, validated configuration, defaults and public snapshot. |
| \`src/sjtuclaw/infrastructure/persistence/provider_settings.py\` | DPAPI-encrypted current-user configuration store. |
| \`src/sjtuclaw/infrastructure/llm/openai_compatible.py\` | Generalized existing Chat Completions adapter. |
| \`src/sjtuclaw/infrastructure/llm/anthropic.py\` | Anthropic Messages request/SSE/tool conversion. |
| \`src/sjtuclaw/runtime/provider_runtime.py\` | Atomic replacements and immutable per-Run leases. |
| \`src/sjtuclaw/runtime/{agent_loop,service,compaction,session_title}.py\` | Pass one lease through all model calls for a Run. |
| \`src/sjtuclaw/bootstrap.py\`, \`entrypoints/gateway.py\` | Composition and authenticated public settings API. |
| \`web/src/{App.tsx,styles.css}\` | Theme persistence and lower-right settings UI. |

## Task 1: Provider Configuration and Credential Vault

**Files:**
- Create: \`src/sjtuclaw/infrastructure/persistence/provider_settings.py\`
- Modify: \`src/sjtuclaw/config.py\`
- Modify: \`src/sjtuclaw/infrastructure/persistence/__init__.py\`
- Create: \`tests/unit/test_provider_settings.py\`
- Modify: \`tests/unit/test_config.py\`

**Interfaces:**
- Produces \`ProviderKind\`, \`ProviderConfiguration\`, \`ProviderPublicSnapshot\`, \`ProviderSettingsStore\`.
- \`ProviderSettingsStore.load() -> ProviderConfiguration | None\`; \`save(configuration) -> None\`.

- [ ] **Step 1: Write the failing configuration and vault tests**

~~~python
def test_provider_defaults_are_explicit() -> None:
    assert ProviderConfiguration.defaults(ProviderKind.ANTHROPIC).base_url == "https://api.anthropic.com"

def test_public_snapshot_never_contains_api_key() -> None:
    snapshot = store.public_snapshot(configuration)
    assert "secret" not in repr(snapshot)
    assert snapshot.key_saved is True

def test_windows_vault_round_trips_with_fake_dpapi() -> None:
    store = ProviderSettingsStore(tmp_path, protector=FakeProtector())
    store.save(configuration)
    assert store.load() == configuration
~~~

- [ ] **Step 2: Run tests to verify RED**

Run: \`py -3.12 -m pytest tests/unit/test_provider_settings.py tests/unit/test_config.py -q\`

Expected: collection fails because provider configuration and vault do not exist.

- [ ] **Step 3: Implement the minimal configuration and vault**

~~~python
class ProviderKind(StrEnum):
    DEEPSEEK = "deepseek"
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"

@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    provider: ProviderKind
    model: str
    base_url: str
    api_key: str = field(repr=False)
~~~

Validate a non-empty model/key and absolute http/https URL. Define a small DataProtector protocol; Windows calls CryptProtectData/CryptUnprotectData through ctypes. Outside Windows, return a process-only store that never writes Key material. Write only encrypted bytes and public non-secret metadata below \`settings.data_dir\`.

- [ ] **Step 4: Run focused tests and static checks**

Run: \`py -3.12 -m pytest tests/unit/test_provider_settings.py tests/unit/test_config.py -q; py -3.12 -m ruff check src/sjtuclaw/config.py src/sjtuclaw/infrastructure/persistence/provider_settings.py\`

Expected: all pass.

- [ ] **Step 5: Commit**

~~~powershell
git add src/sjtuclaw/config.py src/sjtuclaw/infrastructure/persistence/provider_settings.py src/sjtuclaw/infrastructure/persistence/__init__.py tests/unit/test_provider_settings.py tests/unit/test_config.py
git commit -m "feat: add protected provider settings"
~~~

## Task 2: OpenAI-Compatible and Anthropic Adapters

**Files:**
- Create: \`src/sjtuclaw/infrastructure/llm/openai_compatible.py\`
- Create: \`src/sjtuclaw/infrastructure/llm/anthropic.py\`
- Modify: \`src/sjtuclaw/infrastructure/llm/deepseek.py\`
- Modify: \`src/sjtuclaw/infrastructure/llm/__init__.py\`
- Create: \`tests/unit/test_openai_compatible_llm.py\`
- Create: \`tests/unit/test_anthropic_llm.py\`

**Interfaces:**
- Produces \`OpenAICompatibleLLMClient(configuration, *, ids, client|transport)\` and \`AnthropicLLMClient(configuration, *, ids, client|transport)\`, both implementing \`StreamingLLMClient\` and \`aclose()\`.
- Retains \`DeepSeekLLMClient(Settings, ...)\` as a compatibility wrapper.

- [ ] **Step 1: Write failing adapter tests**

~~~python
async def test_openai_stream_maps_usage_and_native_tools() -> None:
    client = OpenAICompatibleLLMClient(configuration, transport=MockTransport(handler))
    events = [event async for event in client.stream(native_request)]
    assert events[-1].response is not None
    assert events[-1].response.usage.total_tokens == 9

async def test_anthropic_stream_maps_text_and_tool_use() -> None:
    client = AnthropicLLMClient(configuration, transport=MockTransport(handler), ids=ids)
    response = [event async for event in client.stream(request)][-1].response
    assert response.tool_calls[0].name == "list_dir"
~~~

Include malformed payload/SSE, authentication failure, usage and no-Key-in-error coverage.

- [ ] **Step 2: Run tests to verify RED**

Run: \`py -3.12 -m pytest tests/unit/test_openai_compatible_llm.py tests/unit/test_anthropic_llm.py -q\`

Expected: collection fails because adapters do not exist.

- [ ] **Step 3: Extract the OpenAI-compatible adapter**

Move Chat Completions serialization, streaming assembler and retries from the current DeepSeek adapter. Preserve native/JSON tools and reasoning-content parsing. Leave DeepSeek defaults in its compatibility wrapper.

- [ ] **Step 4: Implement Anthropic Messages conversion**

Map system messages to the top-level \`system\` field, native tools to \`input_schema\`, \`tool_use\` blocks to \`ToolCall\`, and tool results to Anthropic tool-result blocks. Parse \`content_block_delta\`, \`content_block_stop\`, \`message_delta\` and \`message_stop\`, yielding existing \`LLMStreamEvent\` values.

- [ ] **Step 5: Run adapters plus existing DeepSeek tests**

Run: \`py -3.12 -m pytest tests/unit/test_openai_compatible_llm.py tests/unit/test_anthropic_llm.py tests/unit/test_deepseek_llm.py -q\`

Expected: all pass.

- [ ] **Step 6: Commit**

~~~powershell
git add src/sjtuclaw/infrastructure/llm tests/unit/test_openai_compatible_llm.py tests/unit/test_anthropic_llm.py
git commit -m "feat: add configurable model provider adapters"
~~~

## Task 3: Immutable Provider Leases and Tokenizers

**Files:**
- Create: \`src/sjtuclaw/runtime/provider_runtime.py\`
- Modify: \`src/sjtuclaw/bootstrap.py\`
- Modify: \`src/sjtuclaw/runtime/agent_loop.py\`
- Modify: \`src/sjtuclaw/runtime/compaction.py\`
- Modify: \`src/sjtuclaw/runtime/service.py\`
- Modify: \`src/sjtuclaw/runtime/session_title.py\`
- Create: \`tests/unit/test_provider_runtime.py\`
- Modify: \`tests/unit/test_runtime_host.py\`, \`tests/unit/test_bootstrap_tools.py\`

**Interfaces:**
- Produces \`ProviderLease(configuration, client, token_counter, token_counter_mode, model_profile)\`.
- \`ProviderRuntime.current() -> ProviderPublicSnapshot\`, \`lease() -> ProviderLease\`, \`replace(configuration) -> ProviderPublicSnapshot\`.

- [ ] **Step 1: Write failing lease tests**

~~~python
async def test_replace_changes_next_run_but_not_active_lease() -> None:
    first = manager.lease()
    await manager.replace(openai_configuration)
    second = manager.lease()
    assert first.client is deepseek_client
    assert second.client is openai_client

def test_token_counter_follows_provider_kind() -> None:
    assert manager.lease().token_counter_mode == "deepseek"
~~~

Use fakes to assert a Run, its compaction and title generator all receive the same first client after replacement.

- [ ] **Step 2: Run tests to verify RED**

Run: \`py -3.12 -m pytest tests/unit/test_provider_runtime.py tests/unit/test_runtime_host.py -q\`

Expected: collection or constructor assertions fail because no provider runtime exists.

- [ ] **Step 3: Implement atomic immutable leases**

Build/validate the next client and tokenizer before a short replacement lock; swap one immutable record only after construction succeeds. Close retired clients after their leases finish. Keep Key-bearing configuration inside infrastructure/runtime only.

- [ ] **Step 4: Thread leases through every model call**

Change AgentLoop run/resume, CompactionService and SessionTitleGenerator call sites to accept leased dependencies explicitly. Do not mutate a shared AgentLoop field, because different Sessions can run concurrently.

- [ ] **Step 5: Run runtime regressions**

Run: \`py -3.12 -m pytest tests/unit/test_provider_runtime.py tests/unit/test_runtime_host.py tests/unit/test_bootstrap_tools.py tests/unit/test_agent_loop.py tests/unit/test_compaction.py -q\`

Expected: active and new Runs use distinct asserted fake clients.

- [ ] **Step 6: Commit**

~~~powershell
git add src/sjtuclaw/bootstrap.py src/sjtuclaw/runtime tests/unit/test_provider_runtime.py tests/unit/test_runtime_host.py tests/unit/test_bootstrap_tools.py tests/unit/test_agent_loop.py tests/unit/test_compaction.py
git commit -m "feat: switch model providers between runs"
~~~

## Task 4: Authenticated Gateway Settings API

**Files:**
- Modify: \`src/sjtuclaw/entrypoints/gateway.py\`
- Modify: \`tests/integration/test_gateway_runtime.py\`
- Modify: \`tests/integration/test_gateway_security.py\`

**Interfaces:**
- \`GET /api/v1/provider-settings\` returns \`{ settings: ProviderPublicSnapshot }\`.
- \`PUT /api/v1/provider-settings\` accepts \`{ provider, model, baseUrl, apiKey?, reuseSavedKey }\` and returns a public snapshot.

- [ ] **Step 1: Write failing route tests**

~~~python
def test_provider_settings_requires_cookie(client: TestClient) -> None:
    assert client.get("/api/v1/provider-settings").status_code == 401

def test_provider_settings_response_never_returns_key(authenticated_client: TestClient) -> None:
    body = authenticated_client.put("/api/v1/provider-settings", json=payload).json()
    assert "apiKey" not in repr(body)
    assert body["settings"]["keySaved"] is True
~~~

Cover invalid URL, blank model, missing Key without reuse, immediate replacement, failed replacement retaining old settings, and sanitized error bodies.

- [ ] **Step 2: Run tests to verify RED**

Run: \`py -3.12 -m pytest tests/integration/test_gateway_runtime.py tests/integration/test_gateway_security.py -q\`

Expected: route-not-found or assertion failure.

- [ ] **Step 3: Add Pydantic bodies, serializers and routes**

Protect with the existing cookie dependency. Send typed values to \`ProviderRuntime.replace\`; map validation/replacement failures to the existing safe error envelope. Do not log request bodies.

- [ ] **Step 4: Run route and full Python tests**

Run: \`py -3.12 -m pytest tests/integration/test_gateway_runtime.py tests/integration/test_gateway_security.py -q; py -3.12 -m pytest --ignore=tests/live -q\`

Expected: all pass.

- [ ] **Step 5: Commit**

~~~powershell
git add src/sjtuclaw/entrypoints/gateway.py tests/integration/test_gateway_runtime.py tests/integration/test_gateway_security.py
git commit -m "feat: expose protected provider settings API"
~~~

## Task 5: Theme and Provider Settings UI

**Files:**
- Modify: \`web/src/App.tsx\`
- Modify: \`web/src/styles.css\`

**Interfaces:**
- \`Theme = "light" | "dark" | "system"\` at \`sjtuclaw.theme\`.
- \`ProviderSettingsSnapshot\` and \`saveProviderSettings()\` use the authenticated Gateway API.

- [ ] **Step 1: Add typed theme/provider state**

~~~ts
type Theme = "light" | "dark" | "system";
type ProviderSettingsSnapshot = {
  provider: "deepseek" | "openai_compatible" | "anthropic";
  model: string;
  baseUrl: string;
  keySaved: boolean;
  tokenCounterMode: "deepseek" | "approximate";
};
~~~

Apply resolved theme through \`document.documentElement.dataset.theme\`. Use \`matchMedia("(prefers-color-scheme: dark)")\` only for system mode.

- [ ] **Step 2: Implement the lower-right settings popover**

Render a fixed accessible icon button. Its popover opens upward and contains the theme radio group plus \`API 设置…\`. Reuse existing outside-pointer/Escape handling and keep clear of the composer on narrow screens.

- [ ] **Step 3: Implement the API settings dialog**

Fetch public settings on open. Render Provider/model/Base URL and an empty password input. Never repopulate the Key field; show only \`keySaved\`. Add explicit “保留当前 Key” behavior. Disable submit while saving and show only safe Gateway errors.

- [ ] **Step 4: Add semantic dark-theme tokens**

~~~css
:root[data-theme="dark"] {
  --canvas: #101723;
  --surface: #172131;
  --ink: #edf2fa;
  --line: #2a3a50;
  --wash: #202d40;
}
~~~

Replace remaining light-only surfaces in cards, modals, inputs, Thinking, tool failure states and token cards with semantic variables. Preserve blue/red contrast.

- [ ] **Step 5: Build and browser-check**

Run: \`npm run build\`

Check theme persistence after refresh, desktop/narrow popover placement, readable dark messages/tools, masked Key state, and absence of Key in DOM snapshots/console.

- [ ] **Step 6: Commit**

~~~powershell
git add web/src/App.tsx web/src/styles.css
git commit -m "feat: add theme and provider settings UI"
~~~

## Task 6: Documentation and Full Verification

**Files:**
- Modify: \`README.md\`, \`.env.example\`, \`project_structure.md\`
- Modify: \`docs/superpowers/specs/2026-07-25-theme-and-runtime-provider-settings-design.md\` only for required implementation deviations.

- [ ] **Step 1: Document behavior**

Document Provider modes, model/URL/Key fields, Windows DPAPI persistence, non-Windows process-only fallback, exact/approximate token accounting, and next-Run semantics.

- [ ] **Step 2: Update project structure**

Add each new provider runtime, vault and adapter file to \`project_structure.md\`.

- [ ] **Step 3: Run complete verification**

~~~powershell
py -3.12 -m pytest --ignore=tests/live -q
py -3.12 -m ruff check .
py -3.12 -m mypy src
Set-Location web; npm run build
git diff --check
~~~

Expected: zero failures; the existing Starlette TestClient deprecation warning may remain.

- [ ] **Step 4: Isolated browser QA**

Use a temporary Gateway data directory and disposable bootstrap token. Verify no settings API response contains a Key, configuration restores in a fresh Windows Gateway process, active/new Runs use their expected fake or local-mock provider, and desktop/narrow theme layout works. Remove the temporary data after verification.

- [ ] **Step 5: Commit**

~~~powershell
git add README.md .env.example project_structure.md docs/superpowers/specs/2026-07-25-theme-and-runtime-provider-settings-design.md
git commit -m "docs: explain runtime provider settings"
~~~
