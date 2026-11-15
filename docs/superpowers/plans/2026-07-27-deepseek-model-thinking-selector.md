# DeepSeek Model and Thinking Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在聊天输入框右下角增加一个全局 DeepSeek 胶囊选择器，可切换 V4 Flash / V4 Pro 与思考 / 直答，并让设置持久化且真实进入 DeepSeek 请求。

**Architecture:** 继续复用现有 Provider 设置链路。`ProviderConfiguration` 新增思考开关，Provider 设置存储和 Gateway 负责持久化与传输，`ProviderRuntime.replace()` 保持“只影响后续 lease”的语义，DeepSeek adapter 在流式和非流式请求中显式发送 `thinking.type`。前端复用 Provider GET/PUT，在 composer 中提供单胶囊、向上菜单和失败回滚。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、httpx、pytest、React 19、TypeScript、Vite、CSS。

## Global Constraints

- 以已确认设计文档 `docs/superpowers/specs/2026-07-27-deepseek-model-thinking-selector-design.md` 为准。
- 保留工作区中已有但未提交的 `web/src/App.tsx`、`web/src/SchedulerView.tsx`、`web/src/styles.css` 修改；不得覆盖、回退或无关重构。
- 快捷选择器仅在当前 Provider 为 `deepseek` 时显示。
- 全局更新不得改变活动 Run 已持有的 Provider lease；后续取得的新 lease 使用最新配置。
- Provider 设置加载失败或尚未加载时不猜测 UI 状态，不显示选择器。
- API Key 继续只保存、不回传；快捷更新不要求用户重新输入 Key。
- 旧版持久化文件缺少 `thinking_enabled` 时按 `True` 读取。
- 快捷菜单只提供 `deepseek-v4-flash` 与 `deepseek-v4-pro`；现有 Provider 设置弹窗继续允许自定义模型名。
- 每个任务完成后先看 `git diff --check` 和相关测试；提交前只暂存计划列出的文件，并检查 `git diff --cached`。

## File and Interface Map

- `src/sjtuclaw/config.py`
  - `ProviderConfiguration.thinking_enabled: bool`
  - `ProviderPublicSnapshot.thinking_enabled: bool`
- `src/sjtuclaw/infrastructure/persistence/provider_settings.py`
  - 读写 `thinking_enabled`
  - 旧文件默认 `True`
- `src/sjtuclaw/entrypoints/gateway.py`
  - `ProviderSettingsBody.thinking_enabled`
  - JSON 字段 `thinkingEnabled`
- `src/sjtuclaw/bootstrap.py`
  - 把 Provider 配置中的开关传给 `DeepSeekLLMClient`
- `src/sjtuclaw/infrastructure/llm/deepseek.py`
  - 构造参数 `thinking_enabled`
  - 流式与非流式 payload 写入 `thinking.type`
- `web/src/App.tsx`
  - 扩展 `ProviderSettings`
  - 全局快捷保存函数
  - composer 右下角胶囊和向上菜单
- `web/src/styles.css`
  - 胶囊、菜单、选中态、忙碌态、窄屏布局
- `tests/unit/test_provider_settings.py`
  - 持久化、公开快照、旧文件兼容
- `tests/integration/test_gateway_security.py`
  - Provider GET/PUT 契约与 Key 不泄露
- `tests/unit/test_deepseek_llm.py`
  - 两种思考状态的请求 payload
- `tests/unit/test_provider_runtime.py`
  - 复用现有 lease 隔离回归测试

---

## Task 1: 扩展 Provider 配置、存储与 Gateway 契约

**Files:**

- Modify: `src/sjtuclaw/config.py`
- Modify: `src/sjtuclaw/infrastructure/persistence/provider_settings.py`
- Modify: `src/sjtuclaw/entrypoints/gateway.py`
- Modify: `tests/unit/test_provider_settings.py`
- Modify: `tests/integration/test_gateway_security.py`

### Step 1: 先写 Provider 存储失败测试

- [ ] 在 `tests/unit/test_provider_settings.py` 中把测试配置显式设为关闭思考，并断言往返后仍为关闭：

```python
def _configuration() -> ProviderConfiguration:
    return ProviderConfiguration(
        provider=ProviderKind.OPENAI_COMPATIBLE,
        model="gpt-4.1-mini",
        base_url="https://api.example.test/v1",
        api_key="secret-api-key",
        thinking_enabled=False,
    )
```

- [ ] 在公开快照测试中断言：

```python
assert snapshot.thinking_enabled is False
```

- [ ] 新增旧格式兼容测试。使用现有 `FakeProtector` 写入不含新字段的 JSON，再加载：

```python
def test_store_defaults_legacy_configuration_to_thinking_enabled(tmp_path: Path) -> None:
    protector = FakeProtector()
    legacy = json.dumps(
        {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com",
            "api_key": "secret-api-key",
        }
    ).encode("utf-8")
    (tmp_path / "provider-settings.bin").write_bytes(protector.protect(legacy))

    restored = ProviderSettingsStore(tmp_path, protector=protector).load()

    assert restored is not None
    assert restored.thinking_enabled is True
```

- [ ] 添加 `import json`。

### Step 2: 运行存储测试，确认红灯

- [ ] 运行：

```powershell
py -3.12 -m pytest tests/unit/test_provider_settings.py -q
```

预期：`ProviderConfiguration` 不接受 `thinking_enabled`，或快照缺少该属性。

### Step 3: 实现配置和向后兼容持久化

- [ ] 在 `ProviderConfiguration` 中增加默认字段，保持现有构造调用兼容：

```python
thinking_enabled: bool = True
```

- [ ] 在 `ProviderPublicSnapshot` 中增加：

```python
thinking_enabled: bool
```

- [ ] `ProviderSettingsStore.load()` 构造配置时使用：

```python
thinking_enabled=_optional_bool(payload, "thinking_enabled", default=True),
```

- [ ] 在文件底部增加严格布尔读取帮助函数：

```python
def _optional_bool(payload: dict[str, object], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"saved provider setting {key!r} must be a boolean")
    return value
```

- [ ] `save()` 的受保护 JSON 中加入：

```python
"thinking_enabled": configuration.thinking_enabled,
```

- [ ] `public_snapshot()` 传入：

```python
thinking_enabled=configuration.thinking_enabled,
```

### Step 4: 运行存储测试，确认绿灯

- [ ] 运行：

```powershell
py -3.12 -m pytest tests/unit/test_provider_settings.py -q
```

预期：全部通过。

### Step 5: 先写 Gateway 契约失败测试

- [ ] 在 `tests/integration/test_gateway_security.py` 的 Provider GET 期望中增加：

```python
"thinkingEnabled": True,
```

- [ ] Provider PUT 请求中增加：

```python
"thinkingEnabled": False,
```

- [ ] PUT 后增加：

```python
assert updated.json()["provider"]["thinkingEnabled"] is False
assert providers.configuration.thinking_enabled is False
```

- [ ] 再发一次不含 `thinkingEnabled` 的旧客户端 PUT，并断言仍为 `False`，避免旧前端意外重置用户选择。

### Step 6: 运行 Gateway 测试，确认红灯

- [ ] 运行：

```powershell
py -3.12 -m pytest tests/integration/test_gateway_security.py -q
```

预期：响应缺少 `thinkingEnabled`，PUT 因额外字段失败。

### Step 7: 实现 Gateway 字段

- [ ] 扩展 `ProviderSettingsBody`：

```python
thinking_enabled: bool | None = Field(default=None, alias="thinkingEnabled")
```

- [ ] `update_provider_settings()` 创建配置时加入：

```python
thinking_enabled=(
    body.thinking_enabled
    if body.thinking_enabled is not None
    else existing.thinking_enabled
),
```

- [ ] `_provider_settings_payload()` 返回：

```python
"thinkingEnabled": snapshot.thinking_enabled,
```

### Step 8: 验证 Task 1

- [ ] 运行：

```powershell
py -3.12 -m pytest tests/unit/test_provider_settings.py tests/integration/test_gateway_security.py -q
py -3.12 -m ruff check src/sjtuclaw/config.py src/sjtuclaw/infrastructure/persistence/provider_settings.py src/sjtuclaw/entrypoints/gateway.py tests/unit/test_provider_settings.py tests/integration/test_gateway_security.py
git diff --check
```

预期：测试和 Ruff 全部通过，`git diff --check` 无输出。

### Step 9: 提交 Task 1

- [ ] 仅暂存本任务文件，检查后提交：

```powershell
git add src/sjtuclaw/config.py src/sjtuclaw/infrastructure/persistence/provider_settings.py src/sjtuclaw/entrypoints/gateway.py tests/unit/test_provider_settings.py tests/integration/test_gateway_security.py
git diff --cached --check
git diff --cached
git commit -m "feat: persist DeepSeek thinking mode"
```

---

## Task 2: 把思考模式写入 DeepSeek 请求

**Files:**

- Modify: `src/sjtuclaw/infrastructure/llm/deepseek.py`
- Modify: `src/sjtuclaw/bootstrap.py`
- Modify: `tests/unit/test_deepseek_llm.py`
- Verify: `tests/unit/test_provider_runtime.py`

### Step 1: 先写非流式 payload 失败测试

- [ ] 修改现有 `test_complete_sends_text_messages_and_returns_usage` 的精确 payload，加入：

```python
"thinking": {"type": "enabled"},
```

- [ ] 新增关闭思考的测试，构造客户端时传 `thinking_enabled=False`，捕获请求并断言：

```python
assert json.loads(requests[0].content)["thinking"] == {"type": "disabled"}
```

### Step 2: 先写流式 payload 失败断言

- [ ] 在现有流式测试捕获请求后断言：

```python
assert json.loads(requests[0].content)["thinking"] == {"type": "enabled"}
```

### Step 3: 运行 DeepSeek 测试，确认红灯

- [ ] 运行：

```powershell
py -3.12 -m pytest tests/unit/test_deepseek_llm.py -q
```

预期：默认 payload 缺字段，构造函数不接受 `thinking_enabled`。

### Step 4: 实现 DeepSeek adapter

- [ ] 扩展构造函数：

```python
def __init__(
    self,
    settings: Settings,
    *,
    thinking_enabled: bool = True,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
    ids: IdGenerator | None = None,
) -> None:
```

- [ ] 保存：

```python
self._thinking_enabled = thinking_enabled
```

- [ ] 在 `complete()` 与 `stream()` 两个 payload 中加入相同字段：

```python
"thinking": {
    "type": "enabled" if self._thinking_enabled else "disabled",
},
```

### Step 5: 连接 composition root

- [ ] `bootstrap._build_provider_client()` 创建 DeepSeek 客户端时传入：

```python
thinking_enabled=configuration.thinking_enabled,
```

这样每次 `ProviderRuntime.replace()` 创建的新客户端固定持有当时的设置；活动 lease 继续使用旧客户端。

### Step 6: 验证 Task 2

- [ ] 运行：

```powershell
py -3.12 -m pytest tests/unit/test_deepseek_llm.py tests/unit/test_provider_runtime.py -q
py -3.12 -m ruff check src/sjtuclaw/infrastructure/llm/deepseek.py src/sjtuclaw/bootstrap.py tests/unit/test_deepseek_llm.py tests/unit/test_provider_runtime.py
git diff --check
```

预期：全部通过；`test_provider_switch_only_affects_new_leases` 继续证明活动 lease 不受全局切换影响。

### Step 7: 提交 Task 2

- [ ] 仅暂存本任务文件，检查后提交：

```powershell
git add src/sjtuclaw/infrastructure/llm/deepseek.py src/sjtuclaw/bootstrap.py tests/unit/test_deepseek_llm.py
git diff --cached --check
git diff --cached
git commit -m "feat: send DeepSeek thinking mode"
```

---

## Task 3: 增加 composer 胶囊选择器

**Files:**

- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

### Step 1: 扩展前端 Provider 数据契约

- [ ] 在 `ProviderSettings` 增加：

```ts
thinkingEnabled: boolean;
```

- [ ] 在 `openProviderDialog()` 的 DeepSeek 后备值增加：

```ts
thinkingEnabled: true,
```

- [ ] `saveProviderSettings()` 的 PUT body 增加：

```ts
thinkingEnabled: providerDraft.thinkingEnabled,
```

- [ ] 切换 Provider 类型时保留当前思考值；该字段只影响 DeepSeek，其他 Provider 忽略。

### Step 2: 增加快捷选择器状态和关闭行为

- [ ] 增加状态与引用：

```ts
const [providerQuickOpen, setProviderQuickOpen] = useState(false);
const [providerQuickError, setProviderQuickError] = useState<string | null>(null);
const providerQuickMenu = useRef<HTMLDivElement>(null);
```

- [ ] 增加 `useEffect`：打开时监听 `document.pointerdown` 与 `document.keydown`；点击引用外或按 `Escape` 关闭；清理监听器。

- [ ] 当 `providerSettings?.provider !== "deepseek"` 时关闭菜单并清空快捷错误。

### Step 3: 实现全局快捷保存

- [ ] 增加只接受模型或思考开关的函数：

```ts
const saveDeepSeekQuickSettings = async (
  patch: Partial<Pick<ProviderSettings, "model" | "thinkingEnabled">>,
) => {
  if (!providerSettings || providerSettings.provider !== "deepseek" || providerBusy) return;
  const next = { ...providerSettings, ...patch };
  setProviderBusy(true);
  setProviderQuickError(null);
  try {
    const response = await api<{ provider: ProviderSettings }>("/settings/provider", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: next.provider,
        model: next.model,
        baseUrl: next.baseUrl,
        thinkingEnabled: next.thinkingEnabled,
      }),
    });
    setProviderSettings(response.provider);
    setProviderDraft(response.provider);
  } catch (caught) {
    setProviderQuickError(messageOf(caught));
  } finally {
    setProviderBusy(false);
  }
};
```

不做乐观更新：PUT 失败时胶囊继续显示最后一次成功配置，菜单保持可重试。

### Step 4: 实现单胶囊和向上菜单

- [ ] 在 `.composer__actions` 内、发送按钮前，仅 DeepSeek 时渲染：

```tsx
{providerSettings?.provider === "deepseek" && <div className="provider-quick" ref={providerQuickMenu}>
  <button
    className="provider-quick__trigger"
    type="button"
    aria-haspopup="menu"
    aria-expanded={providerQuickOpen}
    disabled={providerBusy}
    onClick={() => {
      setProviderQuickError(null);
      setProviderQuickOpen((open) => !open);
    }}
  >
    <span>{deepSeekModelLabel(providerSettings.model)} · {providerSettings.thinkingEnabled ? "思考" : "直答"}</span>
    <span aria-hidden="true">⌄</span>
  </button>
  {providerQuickOpen && <div className="provider-quick__menu" role="menu" aria-label="DeepSeek 模型与回答模式">
    <p>模型</p>
    {deepSeekModels.map(({ id, label }) => <button
      type="button"
      role="menuitemradio"
      aria-checked={providerSettings.model === id}
      disabled={providerBusy}
      onClick={() => void saveDeepSeekQuickSettings({ model: id })}
    ><span>{label}</span><span aria-hidden="true">{providerSettings.model === id ? "✓" : ""}</span></button>)}
    <p>回答模式</p>
    {deepSeekThinkingModes.map(({ enabled, label }) => <button
      type="button"
      role="menuitemradio"
      aria-checked={providerSettings.thinkingEnabled === enabled}
      disabled={providerBusy}
      onClick={() => void saveDeepSeekQuickSettings({ thinkingEnabled: enabled })}
    ><span>{label}</span><span aria-hidden="true">{providerSettings.thinkingEnabled === enabled ? "✓" : ""}</span></button>)}
    {providerQuickError && <small className="provider-quick__error" role="alert">{providerQuickError}</small>}
  </div>}
</div>}
```

- [ ] 在模块常量区定义精确选项，并为 `map()` 元素补稳定 `key`：

```ts
const deepSeekModels = [
  { id: "deepseek-v4-flash", label: "V4 Flash" },
  { id: "deepseek-v4-pro", label: "V4 Pro" },
] as const;

const deepSeekThinkingModes = [
  { enabled: true, label: "思考" },
  { enabled: false, label: "直答" },
] as const;

const deepSeekModelLabel = (model: string) =>
  deepSeekModels.find((option) => option.id === model)?.label ?? model;
```

### Step 5: 增加贴近参考图的样式

- [ ] 在 `web/src/styles.css` 增加：

```css
.provider-quick { position: relative; }
.provider-quick__trigger {
  display: flex;
  min-height: 32px;
  align-items: center;
  gap: 7px;
  border: 0;
  border-radius: 999px;
  padding: 7px 11px;
  background: var(--wash);
  color: var(--slate);
  font-size: 12px;
  white-space: nowrap;
}
.provider-quick__trigger > span:first-child {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.provider-quick__trigger:hover:not(:disabled),
.provider-quick__trigger[aria-expanded="true"] {
  background: var(--blue-pale);
  color: var(--blue-strong);
}
.provider-quick__menu {
  position: absolute;
  right: 0;
  bottom: calc(100% + 8px);
  z-index: 30;
  width: 210px;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 7px;
  background: var(--surface);
  box-shadow: var(--shadow);
}
```

- [ ] 补充分组标题、菜单项、选中态、错误态、禁用态。
- [ ] 在现有窄屏媒体查询中允许 `.composer__actions` 收缩，并保证胶囊、引导/停止、发送不横向溢出；必要时缩短胶囊 padding，不改变其单控件形态。

### Step 6: 构建验证

- [ ] 运行：

```powershell
npm run build
```

工作目录：`web`

预期：`tsc -b` 和 Vite 构建通过。

- [ ] 回到项目根目录运行：

```powershell
git diff --check
```

预期：无输出。

### Step 7: 浏览器手工验证

- [ ] 启动现有 Gateway / Web 开发环境。
- [ ] 确认胶囊位于聊天框右下角、发送按钮左侧，外观是一个整体控件。
- [ ] 确认菜单向上展开，点击外部和 `Escape` 均关闭。
- [ ] 逐项切换并刷新页面，验证四种显示：
  - `V4 Flash · 思考`
  - `V4 Flash · 直答`
  - `V4 Pro · 思考`
  - `V4 Pro · 直答`
- [ ] 验证 API 更新失败时旧值不变，菜单显示可读错误。
- [ ] 在 API 设置中切换到非 DeepSeek Provider，确认胶囊隐藏。
- [ ] 用窄屏宽度验证 composer 不横向溢出。

### Step 8: 提交 Task 3

- [ ] 先检查已有未提交改动。只在确认这些改动都是本轮用户已要求的功能后暂存：

```powershell
git diff -- web/src/App.tsx web/src/styles.css
git add web/src/App.tsx web/src/styles.css
git diff --cached --check
git diff --cached
git commit -m "feat: add DeepSeek composer selector"
```

`web/src/SchedulerView.tsx` 不属于本任务，不暂存。

---

## Task 4: 全量回归与验收

**Files:**

- Verify all modified files
- No production changes unless a verification failure identifies a scoped defect

### Step 1: Python 回归

- [ ] 运行定向测试：

```powershell
py -3.12 -m pytest tests/unit/test_provider_settings.py tests/unit/test_deepseek_llm.py tests/unit/test_provider_runtime.py tests/integration/test_gateway_security.py -q
```

- [ ] 运行完整测试：

```powershell
py -3.12 -m pytest -q
```

- [ ] 运行静态检查：

```powershell
py -3.12 -m ruff check src tests
```

预期：全部通过。

### Step 2: Web 回归

- [ ] 运行：

```powershell
npm run build
```

工作目录：`web`

预期：TypeScript 与 Vite 构建通过。

### Step 3: 差异与占位符自检

- [ ] 在项目根目录运行：

```powershell
git diff --check
rg -n "TODO|TBD|PLACEHOLDER|thinkingEnabled|thinking_enabled" src web tests
git status --short
```

- [ ] 检查：
  - `thinkingEnabled` 只用于 HTTP / TypeScript；
  - `thinking_enabled` 只用于 Python / 持久化；
  - 没有未完成占位符；
  - 没有 API Key 出现在响应、日志或测试快照；
  - `SchedulerView.tsx` 既有改动仍完整保留。

### Step 4: 对照验收标准

- [ ] 两个模型、两种回答模式共四种组合均能持久化。
- [ ] DeepSeek 流式与非流式请求均显式发送正确 `thinking.type`。
- [ ] 活动 Run 保持旧 lease；新 lease 使用新配置。
- [ ] 非 DeepSeek Provider 的协议行为不变，快捷选择器隐藏。
- [ ] 胶囊位置、向上菜单、外部点击、`Escape`、失败回滚、窄屏布局均符合设计。

### Step 5: 最终报告

- [ ] 汇报：
  - 修改文件与用户可见行为；
  - 实际运行的测试、构建、静态检查结果；
  - 手工 UI 验证结果；
  - 未覆盖范围与剩余风险；
  - 当前 Git 状态和提交列表。
