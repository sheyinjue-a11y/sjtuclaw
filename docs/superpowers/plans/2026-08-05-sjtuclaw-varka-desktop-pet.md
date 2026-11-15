# SJTUClaw Varka Desktop Pet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Windows 10/11 上随 SJTUClaw Gateway 同时启动浏览器工作台和独立法尔伽桌宠，并让桌宠 Quick Chat 始终连接浏览器当前选中的 Session。

**Architecture:** Gateway 增加进程内 active-session 协调器和双客户端一次性 bootstrap 注册表；浏览器只同步选中 Session，Electron Renderer 轮询该选择并复用现有 Session/Run API 与 SSE。Electron 主进程只负责透明置顶窗口、最小 IPC、位置保存和打开工作台；Python launcher 负责启动、一次重启和 Gateway 退出清理。

**Tech Stack:** Python 3.12、FastAPI、pytest、React 19、TypeScript 7、Vite 8、Vitest 4、Electron、Codex pet spritesheet v2。

## Global Constraints

- 仅支持 Windows 10/11；Electron 缺失或退出不得拖垮 Gateway 与浏览器工作台。
- 固定使用法尔伽 v2 素材；图集必须为 1536×2288、8 列×11 行、单元格 192×208。
- 桌宠窗口固定 520×320；角色显示约 96×104；不可调整大小、不可换色、不可换宠、无指针注视。
- 左键只拖动；右键开关侧边 Quick Chat；靠近屏幕左缘时 Quick Chat 翻到角色右侧。
- Quick Chat 约 344×40；Enter 发送；活动 Run 走 steer，否则走普通 request。
- 浏览器选中 Session 为主来源；无有效选择时取最近未归档 Session；仍无则自动新建。
- 桌宠仅显示最新一条通用提醒；审批只能打开浏览器，不在桌宠中批准或拒绝。
- 桌宠视图模型不得包含 Tool 名称、参数、命令、文件、输出或错误详情。
- 无 live 语音聊天、麦克风、听写、播放或静音功能。
- Electron 意外退出只自动重启一次；第二次退出后停止重启。
- 只做当前 Demo 所需修改；不重构 Runtime、Session、Permission、Attachment、Scheduler 或 Skill。
- 新增源码目录时同步更新根目录 `project_structure.md`。
- 自动测试、静态检查和构建全部完成后，必须把 Windows 手工 Demo 标记为“待用户验收”，逐项给出操作、预期和最短排查。

---

## File Map

- `src/sjtuclaw/runtime/active_session.py`：进程内 Session 选择、最近使用回退和自动新建。
- `src/sjtuclaw/entrypoints/gateway_auth.py`：browser/pet 一次性 bootstrap token 注册表。
- `src/sjtuclaw/entrypoints/gateway.py`：认证 Cookie、active-session API、工作台同步和启动装配。
- `src/sjtuclaw/entrypoints/pet_launcher.py`：Electron 定位、启动、一次重启和清理。
- `web/src/activeSession.ts`：浏览器工作台 active-session API 适配。
- `web/desktop/main.ts`：Electron `BrowserWindow`、窗口位置、IPC 和外部浏览器。
- `web/desktop/preload.cts`：Renderer 可用的最小安全桥。
- `web/src/pet/state.ts`：公开 Session/Run 数据到无敏感字段桌宠视图模型的纯映射。
- `web/src/pet/api.ts`：bootstrap、轮询、SSE、request/steer 和 browser token。
- `web/src/pet/controller.ts`：Session 切换、SSE 生命周期与重连协调。
- `web/src/pet/PetApp.tsx`：法尔伽动画、Quick Chat、提醒和拖动交互。
- `web/src/pet/styles.css`：透明 Overlay、胶囊输入框、提醒和 sprite 动画布局。
- `web/public/pets/varka/`：已验证的固定 v2 素材副本。

---

### Task 1: Active Session Coordinator

**Files:**
- Create: `src/sjtuclaw/runtime/active_session.py`
- Create: `tests/unit/test_active_session.py`

**Interfaces:**
- Consumes: `SessionService.create(title: str = "新会话") -> Session`、`SessionService.list(archived=False) -> tuple[Session, ...]`、`SessionService.load(session_id: str) -> Session`。
- Produces: `ActiveSessionCoordinator.resolve() -> Session`、`ActiveSessionCoordinator.select(session_id: str) -> Session`。

- [ ] **Step 1: 写失败测试，覆盖选择、失效回退和空仓新建**

```python
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sjtuclaw.errors import StoreError
from sjtuclaw.models.session import Session, SessionArchiveReason
from sjtuclaw.runtime.active_session import ActiveSessionCoordinator

NOW = datetime(2026, 8, 5, tzinfo=UTC)


def make_session(session_id: str, title: str) -> Session:
    return Session(session_id, title, (), "", NOW, NOW)


class StubSessions:
    def __init__(self, values: list[Session]) -> None:
        self.values = values
        self.created = 0

    def list(self, *, archived: bool = False) -> tuple[Session, ...]:
        return tuple(item for item in self.values if (item.archived_at is not None) == archived)

    def load(self, session_id: str) -> Session:
        for item in self.values:
            if item.session_id == session_id:
                return item
        raise StoreError("missing")

    def create(self, title: str = "新会话") -> Session:
        self.created += 1
        item = make_session(f"session-{self.created}", title)
        self.values.insert(0, item)
        return item


def test_resolve_uses_selected_session_then_falls_back_to_most_recent() -> None:
    recent = make_session("recent", "最近")
    selected = make_session("selected", "选中")
    sessions = StubSessions([recent, selected])
    coordinator = ActiveSessionCoordinator(sessions)

    assert coordinator.select("selected").session_id == "selected"
    sessions.values = [
        recent,
        replace(
            selected,
            archived_at=NOW,
            archived_reason=SessionArchiveReason.USER_ARCHIVED,
        ),
    ]
    assert coordinator.resolve().session_id == "recent"
    sessions.values = [recent, selected]
    coordinator.select("selected")
    sessions.values = [recent]
    assert coordinator.resolve().session_id == "recent"


def test_resolve_creates_when_no_unarchived_session_exists() -> None:
    sessions = StubSessions([])
    assert ActiveSessionCoordinator(sessions).resolve().title == "新会话"
    assert sessions.created == 1


def test_select_rejects_archived_session() -> None:
    archived = replace(
        make_session("old", "旧会话"),
        archived_at=NOW,
        archived_reason=SessionArchiveReason.USER_ARCHIVED,
    )
    with pytest.raises(ValueError, match="archived"):
        ActiveSessionCoordinator(StubSessions([archived])).select("old")
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `py -3.12 -m pytest tests/unit/test_active_session.py -q`

Expected: FAIL，提示 `sjtuclaw.runtime.active_session` 不存在。

- [ ] **Step 3: 实现最小协调器**

```python
class ActiveSessionCoordinator:
    def __init__(self, sessions: SessionService) -> None:
        self._sessions = sessions
        self._selected_id: str | None = None

    def select(self, session_id: str) -> Session:
        session = self._sessions.load(session_id)
        if session.archived_at is not None:
            raise ValueError("active session cannot be archived")
        self._selected_id = session.session_id
        return session

    def resolve(self) -> Session:
        if self._selected_id is not None:
            try:
                selected = self._sessions.load(self._selected_id)
            except StoreError:
                selected = None
            if selected is not None and selected.archived_at is None:
                return selected
        recent = self._sessions.list(archived=False)
        session = recent[0] if recent else self._sessions.create("新会话")
        self._selected_id = session.session_id
        return session
```

- [ ] **Step 4: 验证协调器测试**

Run: `py -3.12 -m pytest tests/unit/test_active_session.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/sjtuclaw/runtime/active_session.py tests/unit/test_active_session.py
git commit -m "feat: coordinate active desktop session"
```

---

### Task 2: Dual Bootstrap Tokens and Active Session API

**Files:**
- Create: `src/sjtuclaw/entrypoints/gateway_auth.py`
- Create: `tests/unit/test_gateway_auth.py`
- Modify: `src/sjtuclaw/entrypoints/gateway.py:76-120,272-590,720-820`
- Modify: `tests/integration/test_gateway_security.py`

**Interfaces:**
- Consumes: `ActiveSessionCoordinator` from Task 1、现有 `GatewayOptions.bootstrap_token` 和 `require_same_origin`。
- Produces: `BootstrapTokenRegistry.issue(purpose, ttl_seconds=30, token=None) -> str`、`consume(token) -> Literal["browser", "pet"] | None`；`GET/PUT /api/v1/ui/active-session`；`POST /api/v1/auth/bootstrap-token`。

- [ ] **Step 1: 写 token 注册表失败测试**

```python
def test_token_is_one_time_and_expires() -> None:
    now = [10.0]
    registry = BootstrapTokenRegistry(now=lambda: now[0])
    token = registry.issue("pet", ttl_seconds=30, token="p" * 43)
    assert registry.consume(token) == "pet"
    assert registry.consume(token) is None

    expired = registry.issue("browser", ttl_seconds=30, token="b" * 43)
    now[0] = 41.0
    assert registry.consume(expired) is None


def test_purposes_are_not_interchangeable() -> None:
    registry = BootstrapTokenRegistry()
    assert registry.consume(registry.issue("pet")) == "pet"
    assert registry.consume(registry.issue("browser")) == "browser"
```

- [ ] **Step 2: 运行并确认注册表测试失败**

Run: `py -3.12 -m pytest tests/unit/test_gateway_auth.py -q`

Expected: FAIL，提示 `gateway_auth` 不存在。

- [ ] **Step 3: 实现注册表**

```python
BootstrapPurpose = Literal["browser", "pet"]


class BootstrapTokenRegistry:
    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._tokens: dict[str, tuple[BootstrapPurpose, float]] = {}

    def issue(
        self,
        purpose: BootstrapPurpose,
        *,
        ttl_seconds: float = 30.0,
        token: str | None = None,
    ) -> str:
        value = token or secrets.token_urlsafe(32)
        if len(value) < 43 or ttl_seconds <= 0:
            raise ValueError("bootstrap token and TTL must be valid")
        self._tokens[value] = (purpose, self._now() + ttl_seconds)
        return value

    def consume(self, token: str) -> BootstrapPurpose | None:
        record = self._tokens.pop(token, None)
        if record is None or record[1] < self._now():
            return None
        return record[0]
```

- [ ] **Step 4: 添加 Gateway 集成失败测试**

在 `test_gateway_security.py` 新增：

```python
def test_pet_and_browser_cookies_have_separate_capabilities() -> None:
    options = GatewayOptions(bootstrap_token="b" * 43)
    tokens = BootstrapTokenRegistry()
    tokens.issue("browser", token=options.bootstrap_token, ttl_seconds=300)
    pet_token = tokens.issue("pet", token="p" * 43, ttl_seconds=300)
    service = _sessions()
    first = service.create("第一会话")
    second = service.create("第二会话")
    app = create_gateway(sessions=service, options=options, bootstrap_tokens=tokens)

    with (
        TestClient(app, base_url=options.origin) as browser,
        TestClient(app, base_url=options.origin) as pet,
    ):
        browser_exchange = browser.post(
            "/api/v1/auth/bootstrap",
            headers={"Origin": options.origin},
            json={"token": options.bootstrap_token},
        )
        pet_exchange = pet.post(
            "/api/v1/auth/bootstrap",
            headers={"Origin": options.origin},
            json={"token": pet_token},
        )
        assert browser_exchange.status_code == 204
        assert pet_exchange.status_code == 204
        assert pet.post(
            "/api/v1/auth/bootstrap",
            headers={"Origin": options.origin},
            json={"token": pet_token},
        ).status_code == 401
        assert browser.post(
            "/api/v1/auth/bootstrap-token",
            headers={"Origin": options.origin},
            json={"purpose": "browser"},
        ).status_code == 403
        issued = pet.post(
            "/api/v1/auth/bootstrap-token",
            headers={"Origin": options.origin},
            json={"purpose": "browser"},
        )
        assert issued.status_code == 201
        assert issued.json()["purpose"] == "browser"
        assert browser.put(
            "/api/v1/ui/active-session",
            headers={"Origin": options.origin},
            json={"sessionId": second.session_id},
        ).json()["session"]["sessionId"] == second.session_id
        assert pet.get("/api/v1/ui/active-session").json()["session"]["sessionId"] == second.session_id
        assert pet.put(
            "/api/v1/ui/active-session",
            headers={"Origin": options.origin},
            json={"sessionId": first.session_id},
        ).status_code == 403
        assert browser.put(
            "/api/v1/ui/active-session",
            json={"sessionId": first.session_id},
        ).status_code == 403
        assert browser.put(
            "/api/v1/ui/active-session",
            headers={"Origin": options.origin},
            json={"sessionId": "missing"},
        ).status_code == 404
```

Task 1 已覆盖归档/删除选择回退和空列表自动创建；本集成测试覆盖 token 重放、缺 Origin、无效 Session 以及 browser/pet 能力隔离。

- [ ] **Step 5: 改造 Gateway 认证和 API**

```python
CLIENT_COOKIE_NAME = "sjtuclaw_client"


class ActiveSessionBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    session_id: str = Field(min_length=1, max_length=256, alias="sessionId")


class BootstrapTokenBody(BaseModel):
    purpose: Literal["browser"]
```

`create_gateway` 新增可注入参数：

```python
bootstrap_tokens: BootstrapTokenRegistry | None = None,
active_sessions: ActiveSessionCoordinator | None = None,
```

默认注册 `gateway_options.bootstrap_token` 为 300 秒 browser token；bootstrap 成功后同时设置现有 HttpOnly session Cookie 和 HttpOnly `sjtuclaw_client=<purpose>` Cookie。增加 `require_browser_cookie`、`require_pet_cookie`，两者先调用 `require_cookie`，再用 `hmac.compare_digest` 检查客户端 Cookie。

新增路由返回固定公开形状：

```python
@app.get(f"{API_PREFIX}/ui/active-session")
async def get_active_session(_authenticated: None = Depends(require_cookie)) -> dict[str, object]:
    return {"session": _session_summary(active_session_state.resolve())}


@app.put(f"{API_PREFIX}/ui/active-session")
async def put_active_session(body: ActiveSessionBody, request: Request, _browser: None = Depends(require_browser_cookie)) -> dict[str, object]:
    require_same_origin(request)
    return {"session": _session_summary(active_session_state.select(body.session_id))}


@app.post(f"{API_PREFIX}/auth/bootstrap-token", status_code=201)
async def issue_bootstrap_token(body: BootstrapTokenBody, request: Request, _pet: None = Depends(require_pet_cookie)) -> dict[str, object]:
    require_same_origin(request)
    token = token_registry.issue(body.purpose, ttl_seconds=30)
    return {"token": token, "purpose": body.purpose, "expiresInSeconds": 30}
```

将 `StoreError` 和 archived `ValueError` 映射到现有 404/409 错误风格，不返回内部异常文本。

- [ ] **Step 6: 运行认证和安全测试**

Run: `py -3.12 -m pytest tests/unit/test_gateway_auth.py tests/integration/test_gateway_security.py -q`

Expected: PASS；原有单 browser bootstrap 测试保持通过。

- [ ] **Step 7: 提交**

```powershell
git add src/sjtuclaw/entrypoints/gateway_auth.py src/sjtuclaw/entrypoints/gateway.py tests/unit/test_gateway_auth.py tests/integration/test_gateway_security.py
git commit -m "feat: add desktop client gateway coordination"
```

---

### Task 3: Browser Session Synchronization

**Files:**
- Create: `web/src/activeSession.ts`
- Create: `web/src/activeSession.test.ts`
- Modify: `web/src/App.tsx:975-997,1164-1200`

**Interfaces:**
- Consumes: Task 2 的 `GET/PUT /ui/active-session`。
- Produces: `readActiveSession(api) -> Promise<SessionSummary>`、`writeActiveSession(api, sessionId) -> Promise<void>`、`chooseInitialSession(activeId, sessions) -> string | null`。

- [ ] **Step 1: 写纯选择逻辑失败测试**

```typescript
import { describe, expect, it, vi } from "vitest";
import { chooseInitialSession, readActiveSession, writeActiveSession } from "./activeSession";

describe("active session sync", () => {
  it("prefers the gateway active session and falls back to the newest list item", () => {
    const sessions = [{ sessionId: "new" }, { sessionId: "old" }];
    expect(chooseInitialSession("old", sessions)).toBe("old");
    expect(chooseInitialSession("missing", sessions)).toBe("new");
    expect(chooseInitialSession(null, [])).toBeNull();
  });

  it("uses the fixed active-session routes", async () => {
    const api = vi.fn().mockResolvedValue({ session: { sessionId: "s1" } });
    expect((await readActiveSession(api)).sessionId).toBe("s1");
    await writeActiveSession(api, "s2");
    expect(api).toHaveBeenLastCalledWith("/ui/active-session", expect.objectContaining({ method: "PUT" }));
  });
});
```

- [ ] **Step 2: 运行并确认失败**

Run: `Set-Location web; npm test -- --run src/activeSession.test.ts`

Expected: FAIL，提示模块不存在。

- [ ] **Step 3: 实现适配器并接入 App**

```typescript
export type ActiveSessionSummary = { sessionId: string; title?: string };
export type ActiveSessionApi = <T>(path: string, init?: RequestInit) => Promise<T>;

export function chooseInitialSession(
  activeId: string | null,
  sessions: readonly ActiveSessionSummary[],
): string | null {
  return sessions.some((item) => item.sessionId === activeId)
    ? activeId
    : sessions[0]?.sessionId ?? null;
}

export async function readActiveSession(api: ActiveSessionApi): Promise<ActiveSessionSummary> {
  return (await api<{ session: ActiveSessionSummary }>("/ui/active-session")).session;
}

export async function writeActiveSession(api: ActiveSessionApi, sessionId: string): Promise<void> {
  await api("/ui/active-session", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId }),
  });
}
```

在 `App.initialise` 中先交换 fragment，再并行读取 active Session 和 Session 列表；以 `chooseInitialSession` 结果调用现有 `selectSession`。在 `selectSession` 的 `refreshSession(sessionId)` 成功后调用 `writeActiveSession(api, sessionId)`，再写 localStorage；写入失败进入现有错误提示，不静默造成桌宠指向旧 Session。

```typescript
const [active, response, archivedResponse] = await Promise.all([
  readActiveSession(api),
  api<{ sessions: SessionSummary[] }>("/sessions"),
  api<{ sessions: SessionSummary[] }>("/sessions?archived=true"),
]);
const sessionId = chooseInitialSession(active.sessionId, response.sessions);
if (sessionId === null) throw new Error("Gateway 未能建立活动会话");
await selectSession(sessionId);

// selectSession 内，refreshSession 成功以后：
await writeActiveSession(api, sessionId);
window.localStorage.setItem("sjtuclaw.selected-session", sessionId);
```

- [ ] **Step 4: 验证单测和前端构建**

Run: `Set-Location web; npm test -- --run src/activeSession.test.ts; npm run build`

Expected: 测试 PASS；Vite/TypeScript 构建 PASS。

- [ ] **Step 5: 提交**

```powershell
git add web/src/activeSession.ts web/src/activeSession.test.ts web/src/App.tsx
git commit -m "feat: sync workbench active session"
```

---

### Task 4: Electron Build, Window Shell, and Varka Assets

**Files:**
- Create: `web/desktop/main.ts`
- Create: `web/desktop/preload.cts`
- Create: `web/desktop/windowGeometry.ts`
- Create: `web/desktop/windowGeometry.test.ts`
- Create: `web/src/pet/electron.d.ts`
- Create: `web/pet.html`
- Create: `web/tsconfig.desktop.json`
- Create: `web/public/pets/varka/pet.json`
- Create binary: `web/public/pets/varka/spritesheet.webp`
- Modify: `web/package.json`
- Modify: `web/package-lock.json`
- Modify: `web/vite.config.ts`

**Interfaces:**
- Consumes: environment `SJTUCLAW_GATEWAY_ORIGIN`、`SJTUCLAW_PET_BOOTSTRAP_TOKEN`；固定资源 `/pets/varka/pet.json`、`/pets/varka/spritesheet.webp`。
- Produces: `window.sjtuPet` bridge；`clampWindowPosition`、`chatSideForBounds`；`dist-desktop/main.js`、`dist-desktop/preload.cjs`。

- [ ] **Step 1: 写窗口边界失败测试**

```typescript
import { describe, expect, it } from "vitest";
import { chatSideForBounds, clampWindowPosition } from "./windowGeometry";

const workArea = { x: 0, y: 0, width: 1920, height: 1040 };

it("clamps a restored window into the current work area", () => {
  expect(clampWindowPosition({ x: -900, y: 2000 }, { width: 520, height: 320 }, workArea))
    .toEqual({ x: 0, y: 720 });
});

it("places chat to the right only at the left edge", () => {
  expect(chatSideForBounds({ x: 0, width: 520 }, workArea)).toBe("right");
  expect(chatSideForBounds({ x: 800, width: 520 }, workArea)).toBe("left");
});
```

- [ ] **Step 2: 安装依赖并确认测试失败**

Run:

```powershell
Set-Location web
npm install --save-dev electron @types/node
npm test -- --run desktop/windowGeometry.test.ts
```

Expected: 依赖安装成功；测试因 `windowGeometry.ts` 不存在而 FAIL。

- [ ] **Step 3: 实现纯窗口几何函数**

```typescript
export type Point = { x: number; y: number };
export type Size = { width: number; height: number };
export type WorkArea = Point & Size;

export function clampWindowPosition(point: Point, size: Size, area: WorkArea): Point {
  return {
    x: Math.min(Math.max(point.x, area.x), area.x + area.width - size.width),
    y: Math.min(Math.max(point.y, area.y), area.y + area.height - size.height),
  };
}

export function chatSideForBounds(
  bounds: Pick<WorkArea, "x" | "width">,
  area: WorkArea,
): "left" | "right" {
  return bounds.x <= area.x + 24 ? "right" : "left";
}
```

- [ ] **Step 4: 配置双页面和 Electron TypeScript 构建**

`package.json` 增加：

```json
{
  "main": "dist-desktop/main.js",
  "scripts": {
    "build": "tsc -b && vite build",
    "build:desktop": "tsc -p tsconfig.desktop.json"
  }
}
```

`vite.config.ts` 使用 `rollupOptions.input` 同时构建 `index.html` 和 `pet.html`。`tsconfig.desktop.json` 使用 `module: "NodeNext"`、`moduleResolution: "NodeNext"`、`outDir: "dist-desktop"`，只包含 `desktop/main.ts`、`desktop/preload.cts`、`desktop/windowGeometry.ts`，排除 `desktop/**/*.test.ts`。浏览器 `npm run build` 与 Electron `npm run build:desktop` 保持独立，不把 desktop config 加入根 `tsconfig.json` references。
`desktop/main.ts` 引用本地模块时写 `./windowGeometry.js`，让 NodeNext 的源码到输出扩展名保持一致。

```typescript
// vite.config.ts
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const root = fileURLToPath(new URL(".", import.meta.url));
export default defineConfig({
  plugins: [react()],
  build: { rollupOptions: { input: { workbench: `${root}/index.html`, pet: `${root}/pet.html` } } },
});
```

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "rootDir": "desktop",
    "outDir": "dist-desktop",
    "strict": true,
    "skipLibCheck": true,
    "types": ["node"]
  },
  "include": ["desktop/main.ts", "desktop/preload.cts", "desktop/windowGeometry.ts"],
  "exclude": ["desktop/**/*.test.ts"]
}
```

- [ ] **Step 5: 实现 Electron 主进程和 preload 最小桥**

`BrowserWindow` 固定配置：

```typescript
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const moduleDirectory = dirname(fileURLToPath(import.meta.url));
const window = new BrowserWindow({
  width: 520,
  height: 320,
  frame: false,
  transparent: true,
  alwaysOnTop: true,
  resizable: false,
  skipTaskbar: true,
  hasShadow: false,
  backgroundColor: "#00000000",
  webPreferences: {
    preload: join(moduleDirectory, "preload.cjs"),
    contextIsolation: true,
    nodeIntegration: false,
    backgroundThrottling: false,
  },
});
```

主进程只注册固定 IPC：`pet:set-interactive`、`pet:drag-start`、`pet:drag-move`、`pet:drag-end`、`pet:get-chat-side`、`pet:open-workbench`。导航只允许初始 Gateway `pet.html`；`setWindowOpenHandler` 始终返回 `{ action: "deny" }`。`pet:open-workbench` 只接收 token 字符串并拼接已知 Gateway origin，随后 `shell.openExternal(origin + "/#bootstrap=" + encodeURIComponent(token))`。

启动时严格校验 origin 等于 `http://127.0.0.1:<1-65535>`、token 长度至少 43；通过后加载：

```typescript
await window.loadURL(`${origin}/pet.html#bootstrap=${encodeURIComponent(token)}`);
```

校验失败时向 stderr 输出不含 token 的固定错误并退出 Electron。

preload 暴露：

```typescript
contextBridge.exposeInMainWorld("sjtuPet", {
  setInteractive: (active: boolean) => ipcRenderer.send("pet:set-interactive", active),
  startDrag: (x: number, y: number) => ipcRenderer.send("pet:drag-start", x, y),
  moveDrag: (x: number, y: number) => ipcRenderer.send("pet:drag-move", x, y),
  endDrag: () => ipcRenderer.send("pet:drag-end"),
  getChatSide: () => ipcRenderer.invoke("pet:get-chat-side"),
  openWorkbench: (token: string) => ipcRenderer.invoke("pet:open-workbench", token),
});
```

窗口位置保存在 `app.getPath("userData")/varka-window.json`；JSON 只接受有限整数 `x/y`，加载后用当前 display workArea 约束。退出前保存；拖动松手保存。

- [ ] **Step 6: 复制并验证法尔伽素材**

```powershell
New-Item -ItemType Directory -Force web/public/pets/varka | Out-Null
Copy-Item -LiteralPath C:/Users/sunyo/.codex/pets/varka/pet.json -Destination web/public/pets/varka/pet.json
Copy-Item -LiteralPath C:/Users/sunyo/.codex/pets/varka/spritesheet.webp -Destination web/public/pets/varka/spritesheet.webp
py -3.12 C:/Users/sunyo/.codex/skills/hatch-pet/scripts/validate_atlas.py web/public/pets/varka/spritesheet.webp --require-v2
```

Expected: validator exit 0；`pet.json` 的 `id` 为 `varka`、`spriteVersionNumber` 为 `2`、路径为 `spritesheet.webp`。

- [ ] **Step 7: 验证窗口测试和两套构建**

Run: `Set-Location web; npm test -- --run desktop/windowGeometry.test.ts; npm run build; npm run build:desktop`

Expected: 测试 PASS；`dist/pet.html`、`dist-desktop/main.js`、`dist-desktop/preload.cjs` 存在。

- [ ] **Step 8: 提交**

```powershell
git add web/desktop web/src/pet/electron.d.ts web/pet.html web/tsconfig.desktop.json web/public/pets/varka web/package.json web/package-lock.json web/vite.config.ts
git commit -m "feat: add electron pet window shell"
```

---

### Task 5: Safe Pet State and Sprite Animation Model

**Files:**
- Create: `web/src/pet/state.ts`
- Create: `web/src/pet/state.test.ts`

**Interfaces:**
- Consumes: Session 快照的 `sessionId/title/runs/pendingApproval` 和公开 SSE `event.type`；不得保存其余字段。
- Produces: `toPetViewModel(raw) -> PetViewModel`、`reducePetEvent(model, event) -> PetViewModel`、`animationFor(state) -> AnimationRow`、`dragAnimation(deltaX) -> "running-left" | "running-right"`。

- [ ] **Step 1: 写状态、安全投影和动画行失败测试**

```typescript
import { describe, expect, it } from "vitest";
import { animationFor, dragAnimation, reducePetEvent, toPetViewModel } from "./state";

it("maps public run states and approval without retaining tool data", () => {
  const model = toPetViewModel({
    sessionId: "s1",
    title: "演示",
    runs: [{ runId: "r1", status: "started" }],
    pendingApproval: { approvalId: "a1", toolName: "run_command", preview: "secret" },
    messages: [{ toolName: "write_file", content: "secret output" }],
  });
  expect(model).toEqual(expect.objectContaining({ sessionId: "s1", state: "waiting" }));
  expect(JSON.stringify(model)).not.toMatch(/run_command|write_file|secret/);
});

it("maps terminal events to one generic latest reminder", () => {
  const base = toPetViewModel({ sessionId: "s1", title: "演示", runs: [], pendingApproval: null });
  const completed = reducePetEvent(base, { type: "run_completed", runId: "r1" });
  expect(completed.state).toBe("review");
  expect(completed.reminder?.kind).toBe("completed");
  expect(reducePetEvent(completed, { type: "run_cancelled", runId: "r1" }).state).toBe("idle");
  expect(reducePetEvent(base, { type: "approval_required", runId: "r1" }).state).toBe("waiting");
  expect(reducePetEvent(base, { type: "assistant_message", runId: "r1" }).reminder?.kind).toBe("input");
});

it("uses exact v2 rows and horizontal drag direction", () => {
  expect(animationFor("idle")).toMatchObject({ row: 0, frameCount: 6 });
  expect(animationFor("waiting")).toMatchObject({ row: 6, frameCount: 6 });
  expect(animationFor("review")).toMatchObject({ row: 8, frameCount: 6 });
  expect(dragAnimation(-1)).toBe("running-left");
  expect(dragAnimation(1)).toBe("running-right");
});
```

- [ ] **Step 2: 运行并确认失败**

Run: `Set-Location web; npm test -- --run src/pet/state.test.ts`

Expected: FAIL，提示 `state.ts` 不存在。

- [ ] **Step 3: 实现白名单视图模型和状态归约**

```typescript
export type PetState = "idle" | "running" | "waiting" | "failed" | "review" | "waving" | "running-left" | "running-right";
export type PetReminder = { id: string; kind: "input" | "approval" | "completed" | "failed"; text: string };
export type PetViewModel = {
  sessionId: string;
  sessionTitle: string;
  activeRunId: string | null;
  state: Exclude<PetState, "waving" | "running-left" | "running-right">;
  reminder: PetReminder | null;
};
```

`toPetViewModel` 逐字段读取并只构造上述五个属性。优先级：pending approval→`waiting`；started/queued→`running`；最新 failed/interrupted→`failed`；latest completed→`review`；cancelled/cancelled_before_start/无 Run→`idle`。审批文案固定“需要审批，请在工作台处理”；失败文案固定“任务失败，请打开工作台查看”；不得拼入原 payload。

```typescript
function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? value as Record<string, unknown> : {};
}

export function toPetViewModel(raw: unknown): PetViewModel {
  const session = asRecord(raw);
  const runs = Array.isArray(session.runs) ? session.runs.map(asRecord) : [];
  const active = runs.find((run) => run.status === "started" || run.status === "queued");
  const latest = runs.at(-1);
  const approval = session.pendingApproval !== null && typeof session.pendingApproval === "object";
  const status = typeof latest?.status === "string" ? latest.status : null;
  const state = approval
    ? "waiting"
    : active
      ? "running"
      : status === "failed" || status === "interrupted"
        ? "failed"
        : status === "completed"
          ? "review"
          : "idle";
  const reminder = approval
    ? { id: `approval:${String(asRecord(session.pendingApproval).approvalId)}`, kind: "approval" as const, text: "需要审批，请在工作台处理" }
    : state === "failed"
      ? { id: `failed:${String(latest?.runId)}`, kind: "failed" as const, text: "任务失败，请打开工作台查看" }
      : state === "review"
        ? { id: `completed:${String(latest?.runId)}`, kind: "completed" as const, text: "任务已完成" }
        : null;
  return {
    sessionId: typeof session.sessionId === "string" ? session.sessionId : "",
    sessionTitle: typeof session.title === "string" ? session.title : "当前会话",
    activeRunId: active && typeof active.runId === "string" ? active.runId : null,
    state,
    reminder,
  };
}

export function reducePetEvent(model: PetViewModel, event: { type: string; runId: string }): PetViewModel {
  if (event.type === "approval_required") {
    return { ...model, state: "waiting", reminder: { id: `approval:${event.runId}`, kind: "approval", text: "需要审批，请在工作台处理" } };
  }
  if (event.type === "run_completed") {
    return {
      ...model,
      activeRunId: null,
      state: "review",
      reminder: model.reminder?.kind === "input"
        ? model.reminder
        : { id: `completed:${event.runId}`, kind: "completed", text: "任务已完成" },
    };
  }
  if (event.type === "run_failed") {
    return { ...model, activeRunId: null, state: "failed", reminder: { id: `failed:${event.runId}`, kind: "failed", text: "任务失败，请打开工作台查看" } };
  }
  if (event.type === "run_cancelled") return { ...model, activeRunId: null, state: "idle", reminder: null };
  if (event.type === "run_started") return { ...model, activeRunId: event.runId, state: "running", reminder: null };
  if (event.type === "assistant_message") {
    return { ...model, state: "review", reminder: { id: `input:${event.runId}`, kind: "input", text: "有新回复，可继续输入" } };
  }
  return model;
}
```

动画表使用 v2 精确值：row 0 `[280,110,110,140,140,320]`；row 1/2 八帧 `[120,120,120,120,120,120,120,220]`；row 3 四帧 `[140,140,140,280]`；row 5 八帧末帧 240；row 6 六帧末帧 260；row 7 六帧末帧 220；row 8 六帧末帧 280。

```typescript
export type AnimationRow = { row: number; durations: readonly number[]; frameCount: number };

const animations: Record<PetState, AnimationRow> = {
  idle: { row: 0, durations: [280, 110, 110, 140, 140, 320], frameCount: 6 },
  "running-right": { row: 1, durations: [120, 120, 120, 120, 120, 120, 120, 220], frameCount: 8 },
  "running-left": { row: 2, durations: [120, 120, 120, 120, 120, 120, 120, 220], frameCount: 8 },
  waving: { row: 3, durations: [140, 140, 140, 280], frameCount: 4 },
  failed: { row: 5, durations: [140, 140, 140, 140, 140, 140, 140, 240], frameCount: 8 },
  waiting: { row: 6, durations: [150, 150, 150, 150, 150, 260], frameCount: 6 },
  running: { row: 7, durations: [120, 120, 120, 120, 120, 220], frameCount: 6 },
  review: { row: 8, durations: [150, 150, 150, 150, 150, 280], frameCount: 6 },
};
```

- [ ] **Step 4: 验证状态测试**

Run: `Set-Location web; npm test -- --run src/pet/state.test.ts`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add web/src/pet/state.ts web/src/pet/state.test.ts
git commit -m "feat: map safe desktop pet states"
```

---

### Task 6: Pet Gateway Client and Session Controller

**Files:**
- Create: `web/src/pet/api.ts`
- Create: `web/src/pet/api.test.ts`
- Create: `web/src/pet/controller.ts`
- Create: `web/src/pet/controller.test.ts`

**Interfaces:**
- Consumes: Task 2 API、现有 `/sessions/{id}`、`/sessions/{id}/requests`、`/sessions/{id}/runs/{runId}/steer`、`/events`。
- Produces: `PetGatewayClient`；`PetSessionController.start(listener) -> void`、`refresh() -> Promise<void>`、`stop() -> void`、`send(text) -> Promise<void>`、`requestBrowserToken() -> Promise<string>`，向 UI 发布 `PetConnectionSnapshot`。

- [ ] **Step 1: 写 request/steer 路由失败测试**

```typescript
it("sends a request without an active run and steer with an active run", async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response("{}", { status: 202 }));
  const client = new PetGatewayClient("http://127.0.0.1:8765", fetcher);
  await client.send({ sessionId: "s1", activeRunId: null }, "hello");
  expect(fetcher.mock.calls[0][0]).toContain("/sessions/s1/requests");
  await client.send({ sessionId: "s1", activeRunId: "r1" }, "more");
  expect(fetcher.mock.calls[1][0]).toContain("/sessions/s1/runs/r1/steer");
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual(expect.objectContaining({ text: "more" }));
});

it("keeps the draft when the server rejects send", async () => {
  const client = new PetGatewayClient("http://127.0.0.1:8765", vi.fn().mockResolvedValue(new Response("{}", { status: 409 })));
  await expect(client.send({ sessionId: "s1", activeRunId: null }, "keep me")).rejects.toThrow();
});
```

- [ ] **Step 2: 写 Session 切换和 SSE 清理失败测试**

```typescript
class FakeEventSource {
  closed = false;
  onerror: (() => void) | null = null;
  constructor(readonly url: string) {}
  addEventListener(_type: string, _listener: (event: MessageEvent) => void): void {}
  close(): void { this.closed = true; }
}

it("closes the previous EventSource when active Session changes", async () => {
  const sources: FakeEventSource[] = [];
  const client = {
    getActiveSession: vi.fn()
      .mockResolvedValueOnce({ sessionId: "s1", title: "一" })
      .mockResolvedValueOnce({ sessionId: "s2", title: "二" }),
    getSession: vi.fn(async (sessionId: string) => ({
      sessionId,
      title: sessionId,
      runs: [{ runId: sessionId === "s1" ? "r1" : "r2", status: "started" }],
      pendingApproval: null,
    })),
    eventUrl: (sessionId: string, runId: string) => `/sessions/${sessionId}/runs/${runId}/events`,
    send: vi.fn(),
    requestBrowserToken: vi.fn(),
  };
  const controller = new PetSessionController({
    client,
    eventSource: (url) => {
      const source = new FakeEventSource(url);
      sources.push(source);
      return source;
    },
    pollMilliseconds: 1_000,
  });
  await controller.refresh();
  await controller.refresh();
  expect(sources[0].closed).toBe(true);
  expect(sources[1].url).toContain("/sessions/s2/runs/r2/events");
  controller.stop();
  expect(sources[1].closed).toBe(true);
});
```

- [ ] **Step 3: 运行并确认失败**

Run: `Set-Location web; npm test -- --run src/pet/api.test.ts src/pet/controller.test.ts`

Expected: FAIL，两个模块不存在。

- [ ] **Step 4: 实现 Gateway client**

`PetGatewayClient` 所有 fetch 固定 `credentials: "same-origin"`，写请求固定 `Origin` 由浏览器自动生成且使用 JSON。fragment 交换成功后调用 `history.replaceState` 清除 token。发送前再次 `GET /ui/active-session` 和 `GET /sessions/{id}`，避免向刚切走的 Session 发送。

```typescript
async send(target: { sessionId: string; activeRunId: string | null }, text: string): Promise<void> {
  const value = text.trim();
  if (!value) throw new Error("消息不能为空");
  if (target.activeRunId) {
    const id = crypto.randomUUID();
    await this.request(`/sessions/${target.sessionId}/runs/${target.activeRunId}/steer`, {
      method: "POST",
      body: JSON.stringify({ steerId: id, idempotencyKey: id, text: value }),
    });
    return;
  }
  await this.request(`/sessions/${target.sessionId}/requests`, {
    method: "POST",
    body: JSON.stringify({ text: value, attachmentRefs: [], idempotencyKey: crypto.randomUUID() }),
  });
}
```

`requestBrowserToken()` 只读取 `{ token }`；随后由 UI 调用 `window.sjtuPet.openWorkbench(token)`，Renderer 不自行打开任意 URL。

- [ ] **Step 5: 实现 1 秒轮询和单一 SSE 生命周期**

`PetSessionController.refresh()` 执行一次确定性刷新；`start(listener)` 立即调用一次 refresh 并建立 1,000ms interval；`stop()` 清 interval 和 EventSource。每次 refresh：读取 active summary→读取 Session snapshot→调用 Task 5 的 `toPetViewModel`→如 `(sessionId, activeRunId)` 变化则关闭旧 EventSource 并打开新流。每条 SSE 只把 `event.type`、`runId` 传给 `reducePetEvent`，随后异步刷新快照；绝不把 event payload 写入 `PetViewModel`。`error` 事件设置 `connected:false`，下一轮 polling 成功恢复。

```typescript
async refresh(): Promise<void> {
  try {
    const active = await this.client.getActiveSession();
    const raw = await this.client.getSession(active.sessionId);
    const model = toPetViewModel(raw);
    const streamKey = model.activeRunId ? `${model.sessionId}:${model.activeRunId}` : null;
    if (streamKey !== this.streamKey) {
      this.source?.close();
      this.source = null;
      this.streamKey = streamKey;
      if (model.activeRunId) this.connectEvents(model.sessionId, model.activeRunId);
    }
    this.snapshot = { connected: true, model };
    this.listener?.(this.snapshot);
  } catch {
    this.snapshot = { ...this.snapshot, connected: false };
    this.listener?.(this.snapshot);
  }
}

start(listener: (snapshot: PetConnectionSnapshot) => void): void {
  this.listener = listener;
  void this.refresh();
  this.timer = window.setInterval(() => void this.refresh(), this.pollMilliseconds);
}

stop(): void {
  if (this.timer !== null) window.clearInterval(this.timer);
  this.timer = null;
  this.source?.close();
  this.source = null;
}
```

`connectEvents` 为 `run_started`、`assistant_message`、`approval_required`、`run_completed`、`run_failed`、`run_cancelled` 注册具名 listener；`assistant_message` 只表示“有新回复，可继续输入”，不读取正文：

```typescript
private connectEvents(sessionId: string, runId: string): void {
  const source = this.eventSource(this.client.eventUrl(sessionId, runId));
  for (const type of ["run_started", "assistant_message", "approval_required", "run_completed", "run_failed", "run_cancelled"]) {
    source.addEventListener(type, () => {
      this.snapshot = { ...this.snapshot, model: reducePetEvent(this.snapshot.model, { type, runId }) };
      this.listener?.(this.snapshot);
      void this.refresh();
    });
  }
  source.onerror = () => {
    this.snapshot = { ...this.snapshot, connected: false };
    this.listener?.(this.snapshot);
  };
  this.source = source;
}
```

- [ ] **Step 6: 验证 client/controller 测试**

Run: `Set-Location web; npm test -- --run src/pet/api.test.ts src/pet/controller.test.ts`

Expected: PASS；fake EventSource 在 Session 切换和 `stop()` 时均关闭。

- [ ] **Step 7: 提交**

```powershell
git add web/src/pet/api.ts web/src/pet/api.test.ts web/src/pet/controller.ts web/src/pet/controller.test.ts
git commit -m "feat: connect pet to active session"
```

---

### Task 7: Pet Renderer UI and Interaction

**Files:**
- Create: `web/src/pet/main.tsx`
- Create: `web/src/pet/PetApp.tsx`
- Create: `web/src/pet/PetApp.test.tsx`
- Create: `web/src/pet/styles.css`
- Modify: `web/package.json`
- Modify: `web/package-lock.json`

**Interfaces:**
- Consumes: `PetSessionController`、`PetViewModel`、`window.sjtuPet`、Task 5 动画表。
- Produces: `PetApp`；右键 Quick Chat、左键拖动、最新提醒、重连状态和打开工作台。

- [ ] **Step 1: 安装轻量 DOM 测试依赖并写失败测试**

Run: `Set-Location web; npm install --save-dev @testing-library/react @testing-library/user-event jsdom`

```tsx
// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import { PetApp } from "./PetApp";
import type { PetViewModel } from "./state";

const idleModel: PetViewModel = {
  sessionId: "s1",
  sessionTitle: "演示",
  activeRunId: null,
  state: "idle" as const,
  reminder: null,
};

function fakeController(options: { send: (text: string) => Promise<void>; connected?: boolean; model?: PetViewModel }) {
  return {
    start(listener: (snapshot: { connected: boolean; model: PetViewModel }) => void) {
      listener({ connected: options.connected ?? true, model: options.model ?? idleModel });
    },
    stop: vi.fn(),
    send: options.send,
    requestBrowserToken: vi.fn().mockResolvedValue("b".repeat(43)),
  };
}

function fakeBridge() {
  return {
    setInteractive: vi.fn(),
    startDrag: vi.fn(),
    moveDrag: vi.fn(),
    endDrag: vi.fn(),
    getChatSide: vi.fn().mockResolvedValue("left" as const),
    openWorkbench: vi.fn().mockResolvedValue(undefined),
  };
}

it("opens chat on context menu and closes it after a successful send", async () => {
  const send = vi.fn().mockResolvedValue(undefined);
  render(<PetApp controller={fakeController({ send })} bridge={fakeBridge()} />);
  fireEvent.contextMenu(screen.getByLabelText("法尔伽桌宠"));
  const input = screen.getByRole("textbox", { name: "Quick Chat" });
  await userEvent.type(input, "你好{enter}");
  expect(send).toHaveBeenCalledWith("你好");
  expect(screen.queryByRole("textbox", { name: "Quick Chat" })).toBeNull();
});

it("keeps draft and shows reconnect state after failures", async () => {
  const send = vi.fn().mockRejectedValue(new Error("offline"));
  render(<PetApp controller={fakeController({ send, connected: false })} bridge={fakeBridge()} />);
  fireEvent.contextMenu(screen.getByLabelText("法尔伽桌宠"));
  await userEvent.type(screen.getByRole("textbox"), "保留{enter}");
  expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("保留");
  expect(screen.getByText("正在重连")).not.toBeNull();
});

it("dismisses only the current generic reminder", async () => {
  const model = {
    ...idleModel,
    state: "waiting" as const,
    reminder: { id: "approval:r1", kind: "approval" as const, text: "需要审批，请在工作台处理" },
  };
  render(<PetApp controller={fakeController({ send: vi.fn(), model })} bridge={fakeBridge()} />);
  await userEvent.click(screen.getByRole("button", { name: "关闭提醒" }));
  expect(screen.queryByRole("status")).toBeNull();
});
```

- [ ] **Step 2: 运行并确认失败**

Run: `Set-Location web; npm test -- --run src/pet/PetApp.test.tsx`

Expected: FAIL，提示 `PetApp` 不存在。

- [ ] **Step 3: 实现动画和首次问候**

`PetSprite` 用一个 96×104 元素显示 50% 缩放图集，CSS background-size 为 `768px 1144px`，每帧移动 `96px/104px`。React timer 按 `animationFor(state).durations` 逐帧推进；首次 mount 播放一次 `waving`，最长 8 秒后恢复 controller 的业务状态。`prefers-reduced-motion: reduce` 时固定每行动画第一帧。

```tsx
function PetSprite({ state }: { state: PetState }) {
  const animation = animationFor(state);
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    setFrame(0);
    const timer = window.setTimeout(
      () => setFrame((value) => (value + 1) % animation.frameCount),
      animation.durations[frame] ?? animation.durations[0],
    );
    return () => window.clearTimeout(timer);
  }, [animation, frame]);
  return <span className={`pet-sprite pet-sprite--row-${animation.row} pet-sprite--frame-${frame}`} />;
}

const [greeting, setGreeting] = useState(true);
useEffect(() => {
  const timer = window.setTimeout(() => setGreeting(false), 8_000);
  return () => window.clearTimeout(timer);
}, []);
const visibleState = dragState ?? (greeting ? "waving" : snapshot.model.state);
```

- [ ] **Step 4: 实现右键 Quick Chat、提醒和拖动**

角色 `onContextMenu` 阻止系统菜单并切换 Quick Chat；`Escape` 和再次右键关闭；发送成功清空并关闭，失败保留文本。提醒只有一个 `role="status"`，关闭只设本地 dismissed id。

左键 `pointerdown` 记录 `screenX/screenY` 并调用 bridge `startDrag`；移动时调用 `moveDrag`，同时按 deltaX 使用 `running-left/right`；松开调用 `endDrag` 并恢复业务状态。交互元素 enter/leave 调用 `setInteractive(true/false)`，透明空白区域继续穿透。

审批提醒按钮文案固定“打开工作台”；点击时调用 controller `requestBrowserToken()`，再调用固定 bridge。桌宠内不渲染批准/拒绝按钮。

```tsx
const toggleChat = (event: React.MouseEvent) => {
  event.preventDefault();
  setChatOpen((value) => !value);
};

useEffect(() => {
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Escape") setChatOpen(false);
  };
  window.addEventListener("keydown", onKeyDown);
  return () => window.removeEventListener("keydown", onKeyDown);
}, []);

const submit = async (event: React.FormEvent) => {
  event.preventDefault();
  if (!draft.trim() || !snapshot.connected) return;
  try {
    await controller.send(draft);
    setDraft("");
    setChatOpen(false);
    setSendError(null);
  } catch {
    setSendError("发送失败，请重试");
  }
};

const openWorkbench = async () => {
  const token = await controller.requestBrowserToken();
  await bridge.openWorkbench(token);
};

const visibleReminder = snapshot.model.reminder?.id === dismissedReminderId
  ? null
  : snapshot.model.reminder;
```

Quick Chat 内只渲染精简标题、连接状态和单行输入：

```tsx
{chatOpen && <form className={`quick-chat quick-chat--${chatSide}`} onSubmit={submit}>
  <span className="quick-chat__session" title={snapshot.model.sessionTitle}>{snapshot.model.sessionTitle}</span>
  <input aria-label="Quick Chat" value={draft} disabled={!snapshot.connected} onChange={(event) => setDraft(event.target.value)} />
  <span className="quick-chat__connection">{snapshot.connected ? "已连接" : "正在重连"}</span>
</form>}
{visibleReminder && <aside role="status">
  <span>{visibleReminder.text}</span>
  {visibleReminder.kind === "approval" && <button onClick={() => void openWorkbench()}>打开工作台</button>}
  <button aria-label="关闭提醒" onClick={() => setDismissedReminderId(visibleReminder.id)}>×</button>
</aside>}
```

拖动 handler 只响应 `event.button === 0`，右键不会进入拖动：

```tsx
const beginDrag = (event: React.PointerEvent) => {
  if (event.button !== 0) return;
  dragOrigin.current = { x: event.screenX, y: event.screenY };
  event.currentTarget.setPointerCapture(event.pointerId);
  bridge.startDrag(event.screenX, event.screenY);
};
```

- [ ] **Step 5: 实现入口和视觉样式**

`pet.html` 只挂载 `#pet-root` 和 `/src/pet/main.tsx`。Electron 主进程从环境读取 origin/token；Renderer 使用当前页面的 `window.location.origin` 和 fragment 完成交换。缺 token 时显示通用连接错误，不记录 token。

```tsx
const client = new PetGatewayClient(window.location.origin, window.fetch.bind(window));
await client.exchangeBootstrapFragment(window.location.hash);
const controller = new PetSessionController({ client, eventSource: (url) => new EventSource(url) });
createRoot(document.getElementById("pet-root")!).render(
  <StrictMode><PetApp controller={controller} bridge={window.sjtuPet} /></StrictMode>,
);
```

CSS 使用透明根画布、深色半透明胶囊、1px 低对比边线和轻阴影；Quick Chat 宽 344px、高 40px，默认在角色左边，bridge 返回 `right` 时翻转。角色颜色完全来自 spritesheet，不加 filter/hue-rotate。

```css
html, body, #pet-root { width: 100%; height: 100%; margin: 0; overflow: hidden; background: transparent; }
.pet-sprite { width: 96px; height: 104px; display: block; background-image: url("/pets/varka/spritesheet.webp"); background-size: 768px 1144px; background-repeat: no-repeat; }
.pet-sprite--frame-0 { background-position-x: 0; }
.pet-sprite--frame-1 { background-position-x: -96px; }
.pet-sprite--frame-2 { background-position-x: -192px; }
.pet-sprite--frame-3 { background-position-x: -288px; }
.pet-sprite--frame-4 { background-position-x: -384px; }
.pet-sprite--frame-5 { background-position-x: -480px; }
.pet-sprite--frame-6 { background-position-x: -576px; }
.pet-sprite--frame-7 { background-position-x: -672px; }
.pet-sprite--row-0 { background-position-y: 0; }
.pet-sprite--row-1 { background-position-y: -104px; }
.pet-sprite--row-2 { background-position-y: -208px; }
.pet-sprite--row-3 { background-position-y: -312px; }
.pet-sprite--row-5 { background-position-y: -520px; }
.pet-sprite--row-6 { background-position-y: -624px; }
.pet-sprite--row-7 { background-position-y: -728px; }
.pet-sprite--row-8 { background-position-y: -832px; }
.quick-chat { width: 344px; height: 40px; border: 1px solid rgba(255,255,255,.16); border-radius: 20px; background: rgba(22,24,29,.9); box-shadow: 0 8px 24px rgba(0,0,0,.28); }
@media (prefers-reduced-motion: reduce) { .pet-sprite { transition: none; } }
```

- [ ] **Step 6: 验证交互测试和构建**

Run: `Set-Location web; npm test -- --run src/pet/PetApp.test.tsx src/pet/state.test.ts src/pet/api.test.ts src/pet/controller.test.ts; npm run build; npm run build:desktop`

Expected: 测试与两套构建全部 PASS。

- [ ] **Step 7: 提交**

```powershell
git add web/src/pet web/package.json web/package-lock.json
git commit -m "feat: build varka quick chat renderer"
```

---

### Task 8: Python Electron Launcher and Gateway Startup

**Files:**
- Create: `src/sjtuclaw/entrypoints/pet_launcher.py`
- Create: `tests/unit/test_pet_launcher.py`
- Modify: `src/sjtuclaw/entrypoints/gateway.py:2060-2140`
- Modify: `tests/unit/test_gateway_startup.py`

**Interfaces:**
- Consumes: `BootstrapTokenRegistry.issue("pet")`、`GatewayOptions.origin`、`web/node_modules/electron/dist/electron.exe`、`web/dist-desktop/main.js`。
- Produces: `PetLauncher.run() -> None` 和 Gateway helper `_run_pet_when_ready(server, launcher) -> None`；Gateway 启动时 browser + pet 并行，关闭时清理 Electron。

- [ ] **Step 1: 写 launcher 失败测试**

```python
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from sjtuclaw.entrypoints.pet_launcher import PetLauncher


@dataclass
class FakeProcess:
    return_code: int
    terminated: bool = False

    async def wait(self) -> int:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True


class FakeProcessFactory:
    def __init__(self, return_codes: list[int]) -> None:
        self.return_codes = iter(return_codes)
        self.calls: list[SimpleNamespace] = []

    async def __call__(self, electron: str, main: str, *, env: dict[str, str]) -> FakeProcess:
        self.calls.append(SimpleNamespace(electron=electron, main=main, env=env))
        return FakeProcess(next(self.return_codes))


def configured_launcher(
    root: Path,
    *,
    factory: FakeProcessFactory,
    issue: Callable[[], str],
) -> PetLauncher:
    electron = root / "node_modules/electron/dist/electron.exe"
    main = root / "dist-desktop/main.js"
    electron.parent.mkdir(parents=True)
    main.parent.mkdir(parents=True)
    electron.touch()
    main.touch()
    return PetLauncher(
        origin="http://127.0.0.1:8765",
        web_root=root,
        issue_pet_token=issue,
        create_process=factory,
    )


class BlockingProcess:
    def __init__(self) -> None:
        self.terminated = False
        self._stopped = asyncio.Event()

    async def wait(self) -> int:
        await self._stopped.wait()
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self._stopped.set()


def configured_launcher_with_callable(root: Path, create_process: Callable[..., object]) -> PetLauncher:
    electron = root / "node_modules/electron/dist/electron.exe"
    main = root / "dist-desktop/main.js"
    electron.parent.mkdir(parents=True)
    main.parent.mkdir(parents=True)
    electron.touch()
    main.touch()
    return PetLauncher(
        origin="http://127.0.0.1:8765",
        web_root=root,
        issue_pet_token=lambda: "p" * 43,
        create_process=create_process,
    )


def test_missing_electron_returns_without_stopping_gateway(tmp_path: Path) -> None:
    warnings: list[str] = []
    launcher = PetLauncher(
        origin="http://127.0.0.1:8765",
        web_root=tmp_path,
        issue_pet_token=lambda: "p" * 43,
        warn=warnings.append,
    )
    asyncio.run(launcher.run())
    assert warnings == ["Electron desktop pet is unavailable; Gateway will continue"]


def test_crash_restarts_once_with_a_fresh_token(tmp_path: Path) -> None:
    tokens = iter(("a" * 43, "b" * 43))
    factory = FakeProcessFactory(return_codes=[1, 1])
    launcher = configured_launcher(tmp_path, factory=factory, issue=lambda: next(tokens))
    asyncio.run(launcher.run())
    assert len(factory.calls) == 2
    assert factory.calls[0].env["SJTUCLAW_PET_BOOTSTRAP_TOKEN"] != factory.calls[1].env["SJTUCLAW_PET_BOOTSTRAP_TOKEN"]


def test_cancellation_terminates_child(tmp_path: Path) -> None:
    process = BlockingProcess()

    async def create_process(_electron: str, _main: str, *, env: dict[str, str]) -> BlockingProcess:
        assert env["SJTUCLAW_PET_BOOTSTRAP_TOKEN"] == "p" * 43
        return process

    launcher = configured_launcher_with_callable(tmp_path, create_process)

    async def scenario() -> None:
        task = asyncio.create_task(launcher.run())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert process.terminated is True
```

- [ ] **Step 2: 运行并确认失败**

Run: `py -3.12 -m pytest tests/unit/test_pet_launcher.py -q`

Expected: FAIL，提示 `pet_launcher` 不存在。

- [ ] **Step 3: 实现 Windows launcher**

```python
class PetLauncher:
    async def run(self) -> None:
        paths = self._discover()
        if paths is None:
            self._warn("Electron desktop pet is unavailable; Gateway will continue")
            return
        for attempt in range(2):
            env = os.environ.copy()
            env["SJTUCLAW_GATEWAY_ORIGIN"] = self._origin
            env["SJTUCLAW_PET_BOOTSTRAP_TOKEN"] = self._issue_pet_token()
            process = await self._create_process(str(paths.electron), str(paths.main), env=env)
            self._process = process
            try:
                return_code = await process.wait()
            except asyncio.CancelledError:
                process.terminate()
                await process.wait()
                raise
            finally:
                self._process = None
            if return_code == 0:
                return
            if attempt == 0:
                self._warn("Electron desktop pet exited; restarting once")
        self._warn("Electron desktop pet exited twice; restart disabled")
```

`_discover()` 在非 Windows (`sys.platform != "win32"`) 直接返回 None；Windows 只接受两个明确文件：`web/node_modules/electron/dist/electron.exe` 和 `web/dist-desktop/main.js`。token 只放子进程 env，不放 argv 或日志。

- [ ] **Step 4: 改造 Gateway 启动装配并写 wiring 测试**

`run_gateway` 创建同一个 `BootstrapTokenRegistry`，将固定 options token 注册为 browser，用它构造 FastAPI；构造 `PetLauncher(issue_pet_token=lambda: tokens.issue("pet", ttl_seconds=300))`。保留 `_open_bootstrap_page_when_ready`，再启动 `_run_pet_when_ready`；两者都只在 `server.started` 后行动。

```python
tokens = BootstrapTokenRegistry()
tokens.issue("browser", token=gateway_options.bootstrap_token, ttl_seconds=300)
active_sessions = ActiveSessionCoordinator(runtime_application.session_service)
gateway_app = create_gateway(
    sessions=runtime_application.session_service,
    runtime=runtime_application.runtime,
    attachments=runtime_application.attachment_service,
    scheduler=runtime_application.scheduler,
    archive=runtime_application.archive_service,
    workspace=runtime_application.workspace_service,
    downloads=runtime_application.downloads,
    skills=runtime_application.skills,
    compaction=runtime_application.compaction,
    commands=runtime_application.commands,
    model_profile=settings.model_profile,
    providers=runtime_application.llm,
    provider_settings=provider_settings,
    memories=runtime_application.memory_service,
    memory_policy=runtime_application.memory_policy,
    prompts=runtime_application.prompts,
    bootstrap_tokens=tokens,
    active_sessions=active_sessions,
    options=gateway_options,
)
launcher = PetLauncher(
    origin=gateway_options.origin,
    web_root=_web_dist().parent,
    issue_pet_token=lambda: tokens.issue("pet", ttl_seconds=300),
)
browser_task = asyncio.create_task(_open_bootstrap_page_when_ready(server, gateway_options))
pet_task = asyncio.create_task(_run_pet_when_ready(server, launcher))
try:
    await server.serve()
finally:
    browser_task.cancel()
    pet_task.cancel()
    await asyncio.gather(browser_task, pet_task, return_exceptions=True)
    await runtime_application.aclose()
```

`test_gateway_startup.py` 捕获两个 task，断言 `create_gateway` 与 launcher 共用 registry；server 退出时两个 task 均 cancel，runtime `aclose()` 仍执行。

- [ ] **Step 5: 验证 launcher 和 startup 测试**

Run: `py -3.12 -m pytest tests/unit/test_pet_launcher.py tests/unit/test_gateway_startup.py -q`

Expected: PASS；缺 Electron、一次重启、二次停止和取消清理都有断言。

- [ ] **Step 6: 提交**

```powershell
git add src/sjtuclaw/entrypoints/pet_launcher.py src/sjtuclaw/entrypoints/gateway.py tests/unit/test_pet_launcher.py tests/unit/test_gateway_startup.py
git commit -m "feat: launch desktop pet with gateway"
```

---

### Task 9: Documentation, Full Verification, and Manual Demo Handoff

**Files:**
- Modify: `project_structure.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: Tasks 1-8 completed implementation。
- Produces: accurate project map、build/start instructions、automated verification record、明确的“待用户验收”清单。

- [ ] **Step 1: 更新项目结构和启动说明**

`project_structure.md` 增加 `entrypoints/pet_launcher.py`、`entrypoints/gateway_auth.py`、`runtime/active_session.py`、`web/desktop/`、`web/src/pet/`、`web/public/pets/varka/` 及依赖方向。README 增加：

```powershell
Set-Location web
npm ci
npm run build
npm run build:desktop
Set-Location ..
py -3.12 -m sjtuclaw.entrypoints.gateway
```

说明 Windows 启动会同时打开浏览器和桌宠；若 Electron/build 缺失，浏览器仍可使用。

- [ ] **Step 2: 运行素材与定向测试**

```powershell
py -3.12 C:/Users/sunyo/.codex/skills/hatch-pet/scripts/validate_atlas.py web/public/pets/varka/spritesheet.webp --require-v2
py -3.12 -m pytest tests/unit/test_active_session.py tests/unit/test_gateway_auth.py tests/unit/test_pet_launcher.py tests/unit/test_gateway_startup.py tests/integration/test_gateway_security.py -q
Set-Location web
npm test -- --run
npm run build
npm run build:desktop
Set-Location ..
```

Expected: 全部 exit 0；构建产物存在。

- [ ] **Step 3: 运行全量非 live 验证**

```powershell
py -3.12 -m pytest --ignore=tests/live -q
py -3.12 -m ruff check .
py -3.12 -m mypy src tests
```

Expected: 全部 exit 0。若失败，只修复本功能造成的回归，并重跑失败命令及本步骤三条命令。

- [ ] **Step 4: 对照验收范围做静态禁项扫描**

```powershell
rg -n "microphone|speech|voice|live audio|hue-rotate|resize-handle|toolName|toolCall|fileCount" web/src/pet web/desktop
```

Expected: 无语音、调色、缩放或 Tool/文件详情 UI；`toolName/toolCall` 若只出现在恶意输入安全测试 fixture 中可保留，生产代码不得出现。

- [ ] **Step 5: 提交文档和验证所需的最后修正**

```powershell
git add project_structure.md README.md
git commit -m "docs: document varka desktop pet demo"
```

- [ ] **Step 6: 向用户发出 Windows 手工 Demo 提醒，状态必须写“待用户验收”**

最终交付中逐项提供以下内容，每项都写“操作 / 预期 / 最短排查”：

1. 启动 Gateway，确认浏览器工作台和法尔伽同时出现。
2. 拖动法尔伽，重启后确认位置恢复且没有越出工作区。
3. 右键打开侧边 Quick Chat，发送普通请求。
4. 浏览器切换 Session，确认桌宠下一次发送进入新 Session。
5. Run 运行中发送文字，确认作为 steer 追加。
6. 检查 `running`、`waiting`、`review`、`failed` 动画及最新单条提醒。
7. 触发审批，确认桌宠不显示敏感详情，并能打开相同 Session 的工作台。
8. 暂停 Gateway，确认 Quick Chat 禁用并显示“正在重连”。
9. 结束 Electron 一次，确认自动重启；第二次结束后确认不再循环重启，Gateway 仍工作。
10. 检查透明空白鼠标穿透、始终置顶、无任务栏图标，以及靠左屏幕边缘时聊天框翻到右侧。

用户反馈前不得把以上人工项写成 PASS；只报告自动验证结果和“Windows 手工 Demo：待用户验收”。

---

## Final Acceptance Gate

- Gateway browser/pet token 独立、一次性、有限时，客户端能力由 HttpOnly client Cookie 隔离。
- 浏览器切换、关闭、当前 Session 归档/删除和零 Session 都符合 active-session 回退规则。
- Quick Chat 普通 request/steer 路由正确，失败保留草稿，断线禁用发送。
- 桌宠状态与 v2 行映射正确，取消回 idle，只保留最新通用提醒。
- 桌宠视图模型和 UI 无 Tool、命令、文件及输出详情。
- Electron 透明固定窗口、最小 IPC、位置约束、一次崩溃重启均有自动测试或明确手工项。
- Python 非 live 测试、ruff、mypy、Vitest、Web build、desktop build、v2 atlas validator 全部 exit 0。
- Windows 视觉与系统窗口行为明确标记“待用户验收”，直到用户反馈。
