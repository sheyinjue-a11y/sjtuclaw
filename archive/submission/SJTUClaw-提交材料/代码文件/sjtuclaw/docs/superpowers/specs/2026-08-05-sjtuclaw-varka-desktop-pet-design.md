# SJTUClaw 法尔伽桌宠 Demo 设计

## 目标

为 Windows 10/11 上的 SJTUClaw 增加一个独立、透明、始终置顶的法尔伽桌宠。Gateway 启动后同时打开现有浏览器工作台和 Electron 桌宠。桌宠右键打开侧边 Quick Chat，并与浏览器当前选中的 Session 共享消息、Run 和状态。

这是课程展示用 Demo。目标是稳定演示 Codex 桌宠最有辨识度的交互，不追求生产级安装、完整跨平台支持或逐像素复刻。

## 参考基线

设计只读检查了本机 Codex Windows 安装包中的桌宠相关 bundle，包括：

- `avatar-overlay-native-frame`
- `avatar-overlay-native-page`
- `avatar-overlay-page`
- `avatar-overlay-pill-dismiss-button`
- `use-avatar-overlay-selection`
- Electron 主进程中的 `avatarOverlay` 窗口配置

从本机 Codex 实现中采用的原则：

- 无边框、透明、始终置顶、不可缩放、任务栏隐藏的 Overlay 窗口；
- 空白区域鼠标穿透，只让桌宠、Quick Chat 和提醒成为点击区域；
- 约 344×40 的 Quick Chat 单行输入形态；
- `idle`、`running`、`waiting`、`failed`、`review` 状态映射；
- 首次唤醒使用 `waving`；
- 提醒可关闭，等待输入和完成结果通过桌宠状态表达。

本设计不复制 Codex 源码。以下行为是 SJTUClaw 的明确适配：右键打开 Quick Chat、左键只拖动、固定法尔伽、只显示最新提醒、审批回到浏览器处理、移除 live 语音。

## 已确认范围

### 桌宠窗口

- 使用 Electron。
- 首版仅支持 Windows 10/11。
- 窗口透明、无边框、始终置顶、固定尺寸、无任务栏图标。
- 建议 Overlay 逻辑画布为 `520×320`；法尔伽显示尺寸为 `96×104`，保持 `192×208` 单元格比例。
- 左键拖动桌宠；松开后保存窗口位置。
- 透明空白区域使用 Electron 鼠标穿透，避免遮挡桌面。
- 启动时把越界位置约束回当前显示器工作区。
- 右键桌宠打开位于法尔伽侧边的 Quick Chat；靠近屏幕左边时自动翻到右侧。
- `Esc`、再次右键或发送成功后收起 Quick Chat。
- 固定使用法尔伽，不提供宠物选择、刷新或资源目录入口。
- 桌宠颜色与素材保持不变，不提供主题或染色选项。
- 不允许用户调整桌宠大小。
- 不使用 v2 图集的 16 个指针注视方向。

### Quick Chat

- 使用 Codex 启发的单行胶囊输入条，目标宽度约 `344px`、高度约 `40px`。
- 显示当前 Session 的精简标题和连接状态，但不复制完整聊天历史。
- Enter 发送，空文本不能发送。
- 当前 Session 没有活动 Run 时，发送普通请求。
- 当前 Session 有活动 Run 时，复用现有 steer 接口追加要求。
- 发送失败时保留草稿并显示简短错误。
- Gateway 断线时禁用发送，显示“正在重连”。
- 提供“打开工作台”，让默认浏览器定位到当前 Session。

### Session 规则

- 浏览器中的 Session 选择是主要来源。
- 浏览器切换 Session 后，把 Session ID 同步给 Gateway。
- 桌宠定期读取 Gateway 当前 Session；发生变化时切换到对应 Session 的现有 SSE 流。
- 浏览器尚未同步选择时，Gateway 使用最近使用的未归档 Session。
- 没有可用 Session 时，Gateway 自动创建一个 Session。
- 当前 Session 被归档或删除时，重复执行“最近 Session，否则新建”的回退。
- 桌宠不会创建独立会话。

### 状态与提醒

状态只依据公开 Runtime/SSE 状态，不解析 Tool 参数或输出：

- 首次显示：播放一次 `waving`，随后进入当前真实状态；问候最多显示 8 秒。
- 无活动 Run：`idle`。
- Run 正在处理、流式输出或等待 steer 安全点：`running`。
- 需要用户输入或审批：`waiting`。
- Run 失败：`failed`。
- Run 成功且结果尚未被用户处理：`review`。
- 拖动时根据水平移动方向临时使用 `running-left` 或 `running-right`；松开后恢复业务状态。
- Run 被取消：回到 `idle`，不显示失败提醒。

提醒规则：

- 同一时间只显示最新一条重要提醒，不实现通知堆栈或数量角标。
- 支持等待输入、需要审批、成功完成和失败四类精简提醒。
- 提醒可单独关闭；关闭不删除 Session、消息或 Run。
- 普通文字输入和选项回答可以通过 Quick Chat 完成。
- 审批提醒只显示“需要审批”和“打开工作台”，不能在桌宠中允许或拒绝。
- 不显示 Tool 名称、参数、命令、文件名、文件数量、输出或错误详情。

### 启动与退出

- Gateway 就绪后继续打开现有浏览器工作台，并启动 Electron 桌宠。
- Gateway 正常退出时终止 Electron 子进程。
- Electron 意外退出后，Gateway 自动重启一次，并为重启实例生成新的认证凭据。
- 第二次意外退出后停止重启，Gateway 与浏览器继续工作，并在终端输出简短警告。
- Electron 或 Node 依赖缺失时，Gateway 仍正常启动浏览器工作台。

## 非目标

- live 语音聊天、麦克风、语音听写、语音播放与静音。
- macOS 或 Linux 桌宠。
- 桌宠尺寸调整、颜色主题、指针注视和宠物管理。
- 完整聊天历史、附件上传、Markdown 阅读器或 Tool 时间线。
- 通知堆栈、通知数量、跨全部 Session 的活动聚合。
- 在桌宠中暴露 Tool、命令、文件或审批细节。
- 生产级崩溃循环退避、诊断上传、自动更新、签名安装包或系统托盘常驻。
- 重构现有 Runtime、Session、Permission、Attachment、Scheduler 或 Skill 规则。

## 架构

### Electron 主进程

Electron 主进程负责：

- 创建单个 `BrowserWindow`；
- 设置 `frame: false`、`transparent: true`、`alwaysOnTop: true`、`resizable: false`、`skipTaskbar: true`；
- 设置透明背景并关闭后台节流；
- 限制导航和新窗口；
- 根据 Renderer 上报的命中区域切换 `setIgnoreMouseEvents`；
- 处理拖动、位置约束、位置持久化和打开默认浏览器；
- 通过 `contextIsolation: true` 的 preload 暴露最小 IPC；
- 禁用 Renderer 的 Node 集成。

Renderer 不接收任意 Shell 或文件系统能力。preload 只暴露拖动、命中区域、窗口位置和打开工作台所需的固定消息。

### Electron Renderer

Renderer 负责：

- 从法尔伽图集绘制动画；
- 维护 `idle/running/waiting/failed/review/waving/running-left/running-right` 的纯前端状态机；
- 右键 Quick Chat、单条提醒和连接状态；
- 调用 Gateway API；
- 订阅当前 Session 的现有 SSE；
- 不持久化聊天、Cookie、API Key 或 Tool 数据。

建议新增独立入口，而不继续扩大现有 `App.tsx`：

```text
web/
├── desktop/
│   ├── main.ts
│   └── preload.cts
├── pet.html
├── public/pets/varka/
│   ├── pet.json
│   └── spritesheet.webp
└── src/pet/
    ├── main.tsx
    ├── PetApp.tsx
    ├── api.ts
    ├── state.ts
    └── styles.css
```

Electron 构建输出固定为 `web/dist-desktop/main.js` 与 `web/dist-desktop/preload.cjs`，并由 `npm run build:desktop` 生成。使用 `.cts` 保证在 `web/package.json` 的 `"type": "module"` 下 preload 仍以 CommonJS 形式加载。Python 侧新增 `src/sjtuclaw/entrypoints/pet_launcher.py`，只负责定位 Electron、签发启动参数、监督一次重启和正常退出清理。新增桌面源码目录后同步更新根目录 `project_structure.md`。

### Gateway 协调

Gateway 增加一个进程内 `ActiveSessionCoordinator`，不改变 Session 持久化模型。新增接口：

- `GET /api/v1/ui/active-session`
  - 返回有效的当前 Session；必要时执行最近 Session 或新建回退。
- `PUT /api/v1/ui/active-session`
  - 浏览器在选择 Session 后提交 `sessionId`。
- `POST /api/v1/auth/bootstrap-token`
  - 仅限已认证的 Electron 客户端请求 `{ "purpose": "browser" }`；返回 30 秒内有效、只能消费一次的 browser token，用于重新打开工作台。

Demo 使用 1 秒轮询同步当前 Session，避免为一个进程内选择状态新增第二套 SSE。真正的消息与 Run 状态继续使用现有 Session SSE。

浏览器初始加载时也读取 Active Session；因此 Electron 的“打开工作台”只需认证并打开 Gateway 根页面，工作台会恢复相同 Session。

### 双客户端认证

浏览器与 Electron 有不同 Cookie 存储，不能复用当前单次 bootstrap。Gateway 把单个布尔 bootstrap 状态扩展为进程内、短生命周期、一次性 token 注册表：

- Gateway 启动时分别签发 browser token 与 pet token。
- browser token 只进入浏览器 URL fragment。
- pet token 通过 Electron 子进程环境传递，Electron 加载页面时再放入 fragment 完成交换；不放入命令行参数。
- 每个 token 只能交换一次，成功后被删除。
- Electron 简单重启时签发新的 pet token。
- 已认证客户端可请求一次性 browser token，用于桌宠重新打开已关闭的工作台。
- 交换完成后两端获得现有 HttpOnly、SameSite Cookie；API 继续使用 `require_cookie`。

Gateway 仍只监听 `127.0.0.1`，继续执行现有 Origin、Cookie、路径和权限检查。

## 数据流

### 启动

1. Gateway 创建 browser 与 pet 两个一次性 bootstrap token。
2. Gateway 监听成功后打开浏览器 bootstrap URL。
3. Gateway 启动 Electron，并通过子进程环境传递 pet bootstrap token 与 Gateway origin。
4. 两个客户端独立交换 token，并获得各自 Cookie。
5. 浏览器和桌宠读取 Active Session；没有 Session 时 Gateway 创建一个。

### 浏览器切换 Session

1. 现有 React 工作台完成 Session 选择。
2. 浏览器 `PUT /ui/active-session`。
3. 桌宠下一次轮询发现 Session ID 变化。
4. 桌宠关闭旧 Session EventSource，读取新 Session 并连接新 SSE。

### Quick Chat

1. 用户右键法尔伽，打开侧边输入条。
2. 用户输入文字并按 Enter。
3. Renderer 重新读取 Active Session，避免向刚切走的 Session 发送。
4. 无活动 Run 时调用现有普通请求接口；有活动 Run 时调用现有 steer 接口。
5. 成功后清空草稿并收起；失败则保留草稿与错误。
6. 现有 SSE 驱动法尔伽状态和最新提醒。

### 审批

1. SSE 或 Session 快照表明存在 Pending Approval。
2. 法尔伽进入 `waiting`，显示不含敏感细节的提醒。
3. 用户点击“打开工作台”。
4. Electron 请求一次性 browser token并打开工作台。
5. 浏览器读取相同 Active Session，并显示现有完整审批 UI。

## 错误处理

- Electron 不可用：记录警告，保留 Gateway 与浏览器。
- Electron 首次崩溃：重启一次；再次崩溃：停止重启。
- Gateway 断线：保持桌宠显示，禁用 Quick Chat，定时重连。
- Session 无效：回退最近未归档 Session，否则新建。
- 普通消息与 steer 发生竞态：服务端结果为准，保留草稿并刷新 Session 后提示重试。
- SSE 重连：复用现有事件 ID、去重与终态刷新逻辑；桌宠不自行重放消息。
- 窗口越界：按当前显示器 work area 约束位置。
- `pet.json`、图集尺寸或资源路径错误：不打开损坏桌宠，输出明确资源错误。
- 浏览器打开失败：提醒保持可见，终端记录失败；不在桌宠中降级执行审批。

## 测试设计

### 自动测试

Python 单元和 Gateway 集成测试：

- Active Session 的设置、读取、归档/删除回退和自动新建；
- Active Session API 的 Cookie 与 Origin 保护；
- browser/pet bootstrap token 相互独立、一次性消费和过期；
- Electron 重启 token 重新签发；
- Electron 不可用不会终止 Gateway。

Vitest：

- Runtime/SSE 到宠物状态的映射；
- Run 取消回到 `idle`；
- 拖动方向与松开恢复；
- Quick Chat 普通请求/steer 路由；
- Session 变化后切换 SSE；
- 提醒只保留最新一条并可关闭；
- 不把 Tool、命令、文件或输出写入桌宠视图模型；
- 窗口边界约束与左右翻转计算。

构建和静态检查：

```powershell
py -3.12 -m pytest --ignore=tests/live -q
py -3.12 -m ruff check .
py -3.12 -m mypy src tests
Set-Location web
npm test -- --run
npm run build
npm run build:desktop
```

上述命令名是本设计的固定交付接口。真实 Provider/live 测试不属于默认验证。

### Windows 手工 Demo

自动测试、静态检查和构建全部完成后，实施代理必须主动提醒用户开始 Windows 手工 Demo，并提供逐项检查表。每一项都要写清操作、预期结果和失败时的最短排查路径。用户反馈结果前，这一阶段必须标记为“待用户验收”，不能声明已经通过。

提醒中必须包含以下视觉与系统窗口检查：

1. 启动 Gateway，确认浏览器与法尔伽同时出现。
2. 拖动法尔伽，重启后确认位置恢复且未越界。
3. 右键打开侧边 Quick Chat，发送普通请求。
4. 在浏览器切换 Session，确认桌宠下一次发送进入新 Session。
5. Run 中发送文字，确认走 steer。
6. 检查 `running`、`waiting`、`review` 和 `failed` 动画与最新提醒。
7. 触发审批，确认桌宠不显示敏感细节且能打开工作台。
8. 临时停止 Gateway，确认 Quick Chat 禁用并显示重连。
9. 结束 Electron 一次，确认自动重启；再次结束，确认不再循环重启。

如果运行环境无法可靠自动检查透明窗口、置顶、鼠标穿透或多显示器行为，这些项目明确交给用户手工验收，不把未执行结果声明为通过。

实施代理收到用户反馈后，应汇总哪些项目通过、失败或未执行。失败项进入定向排查和修复；未执行项继续列为交付风险，不影响已通过的自动测试结论。

## 验收标准

- Windows 上启动 Gateway 后，同时出现现有工作台与法尔伽桌宠。
- 桌宠透明、置顶、固定尺寸、任务栏隐藏，透明区域不阻挡桌面点击。
- 左键拖动、位置保存和越界回正可用。
- 右键 Quick Chat 能向浏览器当前 Session 发送普通请求或 steer。
- 浏览器关闭或尚未选择 Session 时，桌宠使用最近 Session；没有则新建。
- 法尔伽按公开 Run 状态播放正确动画。
- 只显示最新重要提醒；审批必须回到浏览器。
- 桌宠 UI 和视图模型不包含 Tool、命令、文件或输出详情。
- live 语音、缩放、注视、主题、宠物管理和通知堆栈均不存在。
- Electron 不可用或第二次崩溃不会拖垮 Gateway。
- 相关自动测试、静态检查和前端/Electron 构建通过；未执行的 Windows 手工检查被明确列出。
- 自动验证结束后，实施代理主动发送 Windows 手工 Demo 提醒；用户确认前不把手工验收标记为通过。

## 潜在风险

- Electron 增加 Node 依赖和桌面构建步骤；Demo 假设仓库已执行 `npm ci`。
- 透明窗口、鼠标穿透和多显示器缩放只能在真实 Windows 桌面上最终确认。
- 双客户端 bootstrap 修改安全敏感，必须保持一次性、短生命周期和 loopback/Origin/Cookie 边界。
- 1 秒 Active Session 轮询存在最多约 1 秒同步延迟；对 Demo 可接受。
- 法尔伽图集约 3 MB，会增加仓库和前端构建产物体积。
