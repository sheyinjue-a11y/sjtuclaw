# 法尔伽独立迷你会话窗口设计

## 目标

右键法尔伽时打开一个独立的 Electron 迷你会话窗口，显示最近使用的一个有效 Session 的最近消息并允许继续发送。桌宠透明窗口继续只负责角色、拖动和紧邻提醒，聊天窗口不再占用桌宠命中区域。

本设计覆盖《2026-08-05-sjtuclaw-varka-desktop-pet-design.md》中“右键展开单行 Quick Chat”和“不显示历史”的冲突条款。其他安全、Session、动画和进程生命周期要求保持不变。

## 已确认行为

- 左键只拖动桌宠。
- 右键创建或聚焦一个独立迷你会话窗口。
- 同时最多存在一个迷你会话窗口；用户关闭后可再次右键创建。
- 窗口目标尺寸为 `480×600` 逻辑像素，使用普通系统边框，不始终置顶；尺寸和初始位置约束在桌宠所在显示器工作区内。
- 窗口显示当前活动 Session。若浏览器尚未选择有效 Session，沿用 Gateway 的“最近使用的未归档 Session，否则新建”回退规则。
- 消息列表过滤后最多显示最后 10 条，按时间正序排列。
- 只显示 `user` 与 `assistant` 的非空纯文本内容。
- 不显示 system、tool、reasoning、tool calls、结构化结果、资源、附件细节或审批细节。
- 底部使用多行输入框；Enter 发送，Shift+Enter 换行。空文本不可发送。
- 发送时复用现有普通请求/active-run steer 路由；成功后清空草稿，失败时保留草稿并显示简短错误。
- “任务已完成”等单条提醒位于桌宠正下方，间距约 `8px`，在 `520×320` 桌宠窗口内水平约束。
- 提醒主体不接收鼠标；仅关闭按钮可交互，因此提醒显示期间仍可拖动桌宠。

## 窗口架构

Electron 主进程维护两个独立窗口引用：

1. `petWindow`：现有透明、无边框、始终置顶的 `520×320` 桌宠窗口。
2. `chatWindow`：新增普通、固定目标尺寸的迷你会话窗口。

桌宠 Renderer 的右键事件不再切换内嵌表单，而是通过最小 preload IPC `pet:open-chat` 通知主进程。主进程执行单例逻辑：

- `chatWindow` 存在时调用 `show()`、`restore()`（若最小化）和 `focus()`；
- 不存在时创建窗口，加载 `/pet-chat.html`，在 `ready-to-show` 后显示；
- 窗口关闭时清空引用；
- Gateway/Electron 退出时两个窗口随进程关闭，既有“一次崩溃重启”策略不变。

聊天窗口不成为桌宠窗口的模态子窗口，避免关闭、焦点和始终置顶行为互相耦合。

## Renderer 结构

新增独立入口 `web/pet-chat.html` 与聚焦的 `web/src/pet-chat/` 模块：

- `main.tsx`：启动 React、验证现有 Electron Cookie 会话并挂载窗口。
- `ChatApp.tsx`：标题、连接状态、消息列表、草稿和发送交互。
- `messages.ts`：把 Session 快照转换为最多 10 条安全纯文本消息。
- `controller.ts`：读取 active Session、轮询快照、发送消息并发布连接状态。
- `styles.css`：实现参考图的紧凑浅色窗口、可滚动消息区和底部输入区。

现有 `PetGatewayClient` 继续负责 Cookie 请求、active Session、Session 快照和发送；必要时只扩展明确的类型，不复制请求实现。

## 数据流

### 打开窗口

1. 用户在桌宠精灵上右键。
2. Pet Renderer 阻止默认菜单并发送 `pet:open-chat`。
3. Electron 主进程创建或聚焦 `chatWindow`。
4. Chat Renderer 使用与桌宠相同的 Electron session Cookie 请求 `GET /api/v1/ui/active-session`。
5. 读取 `GET /api/v1/sessions/{sessionId}`，过滤并显示最后 10 条用户/助手文本。

### 更新消息

- 窗口打开期间每秒刷新 active Session 与 Session 快照。
- active Session 变化时切换标题和消息列表。
- 消息变化后列表自动滚动到底部。
- 轮询失败时保留最后一次成功内容并显示“正在重连”，成功后自动恢复。
- 不新增第二套 Gateway SSE 协议；一秒刷新足以满足迷你窗口 Demo。

### 发送

1. 用户按 Enter；Shift+Enter 仅换行。
2. Controller 重新读取 active Session 和 active run，避免发到刚切走的 Session。
3. 有 active run 时调用 steer；否则创建普通 request。
4. 成功后清空草稿并立即刷新；失败时保留草稿。

## 提醒与拖动

现有提醒使用 `left: 0; bottom: 0`，在高 DPI 和固定大透明窗口中会与精灵相距很远。新布局根据精灵左右位置计算提醒锚点，将提醒放在精灵底边下方约 `8px`，并限制在窗口横向边界内。

提醒容器设置为不命中鼠标，关闭按钮单独启用命中并在 hover 期间切换 Electron interactivity。精灵本身的 pointer enter、pointer capture 和拖动 IPC 保持独立，提醒出现与否不改变拖动路径。

## 安全边界

- Chat Renderer 保持 `contextIsolation: true`、`nodeIntegration: false`、`sandbox: true`。
- 聊天窗口复用 Electron 默认 session 中现有的 HttpOnly SameSite Cookie，不向 URL、DOM 或 preload 暴露 Cookie。
- 新窗口只允许 Gateway loopback origin 的 `/pet-chat.html`，阻止外部导航和新窗口。
- preload 仅给桌宠暴露无参数的 `openChat()`，不暴露任意 URL、Shell、文件系统或工具数据。
- 消息转换层采用白名单字段，仅输出 `role`、`content` 和稳定标识；忽略其余数据。
- 审批仍只能打开浏览器工作台，迷你窗口不显示审批或工具详情。

## 错误处理

- Chat 窗口创建失败：桌宠和 Gateway 继续工作，主进程记录简短错误。
- Gateway 断线：保留最后 10 条消息、禁用发送、显示正在重连。
- Session 失效：下一次轮询重新执行 active Session 回退。
- 发送失败：保留草稿并显示通用错误，不暴露服务器或工具细节。
- 窗口已存在：仅恢复并聚焦，不重复创建。

## 测试与验收

自动测试覆盖：

- 消息白名单过滤、顺序和最多 10 条限制。
- system/tool/reasoning/tool calls/结构化内容不进入迷你窗口模型。
- active Session 切换、断线保留快照和发送后的刷新。
- Enter 发送、Shift+Enter 换行、空文本禁用、失败保留草稿。
- 右键调用 `openChat()`，不再渲染内嵌 Quick Chat。
- Electron 聊天窗口单例、关闭后可重建、导航限制和工作区约束。
- 提醒锚点位于精灵下方，提醒出现时桌宠拖动仍触发。

人工 Windows Demo 验收：

- 右键桌宠打开约 `480×600` 的独立小窗口，再次右键只聚焦同一窗口。
- 显示目标 Session 最近 10 条用户/助手消息；发送后消息刷新。
- 工具、推理、审批和附件细节均不可见。
- “任务已完成”紧贴桌宠正下方，提醒显示期间仍可左键拖动桌宠。
- 多显示器和高 DPI 下两个窗口均不越出工作区。

