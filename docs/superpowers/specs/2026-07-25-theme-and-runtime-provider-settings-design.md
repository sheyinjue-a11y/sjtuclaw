# 深色主题与运行时 Provider 设置设计

日期：2026-07-25

## 1. 目标与边界

在不改变会话、工具、审批和流式协议的前提下，增加：

- 亮色、深色、跟随系统三种主题；入口位于页面右下角，向上展开。
- Gateway 内即时切换 DeepSeek、OpenAI 兼容和 Anthropic Provider；保存后下一条请求立即生效。
- Windows 当前用户范围内持久保存 Provider、模型、Base URL 与 API Key。
- DeepSeek 继续使用现有精确 tokenizer；其他 Provider 使用现有近似 tokenizer。

不做：多账号管理、云端同步、Key 导出、浏览器保存 Key、旧会话重放时重选 Provider，或为不支持的第三方协议做兼容层。

## 2. 主题与右下角设置

### 2.1 主题

前端新增 `light`、`dark`、`system` 三种主题值。主题只保存到浏览器 `localStorage`，不包含任何 Provider 或密钥数据。

根节点使用 CSS 自定义属性提供颜色令牌。现有布局、消息层级、错误色和可见滚动条沿用，只替换变量值；深色模式保证用户气泡、Agent 气泡、Thinking、工具失败态和表单焦点仍可区分。

### 2.2 入口

页面右下角提供固定的圆形“设置”按钮；弹层向上展开，避免覆盖发送按钮。弹层包含：

1. 主题：浅色 / 深色 / 跟随系统。
2. `API 设置…`：打开独立模态框。

窄屏时保持同一入口，并将弹层约束在视口内。键盘焦点、Escape 关闭与 `aria-expanded` 均按已有菜单模式实现。

## 3. Provider 设置与安全边界

### 3.1 设置字段

`API 设置` 模态框显示：

- Provider：`DeepSeek`、`OpenAI 兼容`、`Anthropic`。
- 模型名：可编辑，按 Provider 填入推荐默认值。
- Base URL：可编辑，按 Provider 填入端点默认值。
- API Key：密码输入框；已保存时只显示掩码状态，绝不回传完整值。

DeepSeek 默认保留现有模型和 URL；OpenAI 兼容默认使用 Chat Completions 路径；Anthropic 默认使用 Messages API 根地址。用户选择 Provider 时仅预填尚未手工修改的模型与 URL。

### 3.2 保存与读取

Gateway 增加仅限 loopback、现有 Cookie 认证下的 Provider 设置读取与更新 API。

- `GET` 只返回 Provider、模型、Base URL、是否已保存 Key、token 计数模式和当前生效状态；绝不返回 Key。
- `PUT` 接受一次 Key、Provider、模型和 Base URL；校验 URL 为 `http/https`，模型非空，Key 非空或明确复用已保存 Key。
- Gateway 不记录 Key，不在错误、SSE、Session、下载或浏览器日志中回显 Key。

持久化采用 Windows DPAPI 加密的小型本地配置文件，密文只能由当前 Windows 用户解密；不新增第三方依赖，不写 `.env`，也不写浏览器存储。非 Windows 环境不伪造安全保存能力：设置页说明仅本次进程有效，并要求重新输入 Key。

## 4. 即时切换运行时

新增一个 Provider 运行时管理器，持有当前不可变配置与对应 LLM Client。

1. 用户保存设置后，Gateway 验证并在内存构造新 Client。
2. 构造成功后原子替换“下一次请求使用的 Client”，再持久化加密配置。
3. 已开始的 Run 保留其开始时绑定的 Client；新的 Gateway 请求、排队请求开始执行时使用最新 Client。
4. 新 Client 构造或健康检查失败时，不替换旧 Client，也不覆盖已保存配置；页面展示安全错误。

为避免在运行中的 AgentLoop 中替换依赖，`RuntimeHost` 每次启动 Run 时从管理器取得 Client 快照，并将该快照传给该 Run、标题生成和后续工具循环。换 Provider 不取消正在运行的请求。

## 5. 协议适配与 Token

### 5.1 DeepSeek / OpenAI 兼容

现有 DeepSeek 适配器抽取为通用 OpenAI Chat Completions 适配器，保留 DeepSeek 的工具调用、流式解析与重试行为。DeepSeek 配置继续选择现有精确 `DeepSeekTokenCounter`。

OpenAI 兼容模式复用同一适配器与 Chat Completions 协议，按 Provider 默认使用 JSON 或 native tools 的既有策略；token 计数使用 `ApproximateTokenCounter`。服务端实际返回 usage 时仍照常显示。

### 5.2 Anthropic

新增 Anthropic Messages 适配器，负责：

- `x-api-key`、`anthropic-version` 请求头；
- Messages 请求/响应与 SSE 流式事件转换；
- 文本块与工具调用块转换为现有 `LLMResponse` / `LLMStreamEvent`；
- Provider usage 映射到现有 `LLMUsage`。

Anthropic 使用近似 tokenizer。若某个模型或工具调用格式不兼容，Run 以现有错误事件结束，不能静默回退到另一 Provider。

## 6. 错误处理

- 保存失败：继续使用旧 Provider；表单保留非敏感字段，Key 输入框清空。
- Gateway 重启：从 DPAPI 配置恢复；恢复/构造失败时保留启动期 `.env` 配置作为回退，并给前端明确状态。
- Key 无法解密：不删除密文，报告“当前 Windows 用户无法读取已保存凭据”，允许用户覆盖保存。
- Base URL、网络或认证错误：不回显 Key、Authorization 或完整请求头。
- 主题切换失败：回退亮色，不能影响聊天或 Provider 设置。

## 7. 测试与验收

自动化：

- 配置 API 不返回 Key；非法 URL/空模型/缺 Key 被拒绝。
- DPAPI 抽象的加密、读取失败与非 Windows 回退使用可替换 fake 测试。
- 保存后下一 Run 使用新 Client；已开始 Run 继续使用旧 Client；新 Client 初始化失败不替换旧 Client。
- OpenAI 兼容和 Anthropic 的请求、流式文本、工具调用、usage、错误映射测试。
- DeepSeek 仍选择精确 tokenizer；其他 Provider 选择近似 tokenizer。
- 主题选择、API 设置表单和 Key 不回显的前端构建/浏览器验收。

验收：

- 右下角可切换亮色、深色、跟随系统，刷新后主题仍在。
- 输入 Provider、模型、URL、Key 后，下一条新消息即时走新配置；正在输出的消息不中断。
- 重启 Gateway 后，在同一 Windows 用户下自动恢复设置。
- 浏览器、Gateway API、Session 文件和日志均找不到明文 Key。
- DeepSeek Token 展示保持精确模式；其他 Provider 标示估算模式。
