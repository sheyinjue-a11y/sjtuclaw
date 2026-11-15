# DeepSeek 全局模型与思考模式选择器设计

日期：2026-07-27
状态：待用户审核

## 目标

在聊天输入框右下角增加一个胶囊式 DeepSeek 选择器，让用户直接切换：

- 模型：`deepseek-v4-flash`、`deepseek-v4-pro`
- 回答模式：思考、直答

选择结果是全局 Provider 设置，保存后用于后续开始执行的 Run，并在应用重启后保留。

## 非目标

- 不为每个会话单独保存模型或思考模式。
- 不允许为单条排队请求单独覆盖 Provider 设置。
- 不改变正在运行的 Run 所使用的 Provider lease。
- 不增加思考强度档位；本次只提供思考开关。
- 不修改其他 Provider 的模型选择体验。

## 用户界面

DeepSeek 是当前 Provider 时，在聊天框右下角、发送按钮左侧显示一个胶囊式选择器。它不是两个独立按钮。

胶囊根据当前设置显示以下四种状态之一：

- `V4 Flash · 思考⌄`
- `V4 Flash · 直答⌄`
- `V4 Pro · 思考⌄`
- `V4 Pro · 直答⌄`

点击胶囊后，菜单向上展开，包含两个分组：

1. 模型
   - V4 Flash
   - V4 Pro
2. 回答模式
   - 思考
   - 直答

当前选择有明确选中状态。点击菜单外区域或按 `Esc` 关闭菜单。保存期间选择器显示忙碌状态并禁止重复操作。

当前 Provider 不是 DeepSeek 时不显示该选择器，继续通过现有 Provider 设置窗口管理其他服务。

## 全局行为

每次选择模型或回答模式后，前端立即调用现有 Provider 设置更新接口并持久化完整配置。

- 正在运行的 Run：继续使用创建 Run 时取得的旧 Provider lease。
- 尚未开始执行的排队请求：开始执行时读取最新全局 Provider 配置。
- 后续新请求、定时任务和通过同一 Gateway 启动的 Run：使用最新全局 Provider 配置。

更新成功后刷新胶囊文字和选中状态。更新失败时保留原设置，并在现有错误区域显示失败原因。

## 配置与 API

在 `ProviderConfiguration` 和公开 Provider 设置快照中增加：

```text
thinking_enabled: bool
```

HTTP 使用现有 `/api/v1/settings/provider`：

- GET 响应增加 `thinkingEnabled`
- PUT 请求接受 `thinkingEnabled`

默认值为 `true`，与 DeepSeek 当前 API 默认行为一致。读取旧版 Provider 设置文件时，如果字段不存在，也按 `true` 处理，避免迁移失败。

Provider 设置文件继续使用现有 DPAPI 保护方式。新增布尔字段不改变 API Key 的保存和返回规则。

## DeepSeek 请求协议

DeepSeek 的流式和非流式 Chat Completions 请求都必须明确发送：

```json
{"thinking": {"type": "enabled"}}
```

或：

```json
{"thinking": {"type": "disabled"}}
```

快捷选择器只会把模型字段切换为：

- `deepseek-v4-flash`
- `deepseek-v4-pro`

现有 Provider 设置窗口仍可填写自定义模型名；遇到自定义 DeepSeek 模型时，胶囊显示原始模型名。

现有 `reasoning_content` 解析与工具调用回放逻辑保持不变。DeepSeek 官方文档说明两种 V4 模型都支持思考与直答模式：

- https://api-docs.deepseek.com/guides/thinking_mode
- https://api-docs.deepseek.com/quick_start/pricing

## 组件边界

### 前端

`App.tsx`：

- 读取公开 Provider 设置。
- 控制胶囊菜单开关、键盘关闭和点击外部关闭。
- 调用 Provider PUT 接口。
- 成功后同步全局状态，失败时回滚显示。

`styles.css`：

- 提供胶囊、向上菜单、分组、选中态、忙碌态和移动端布局。

### Gateway 与持久化

`gateway.py`：

- 校验和传递 `thinkingEnabled`。
- 在公开设置响应中返回该字段。

`provider_settings.py`：

- 持久化思考开关。
- 兼容旧版缺少该字段的设置文件。

### Provider Runtime 与 DeepSeek Adapter

`ProviderConfiguration` 持有全局思考设置。`ProviderRuntime.replace()` 保持现有 lease 隔离语义：替换只影响未来 lease。

`DeepSeekLLMClient` 在流式和非流式请求体中写入思考参数。OpenAI Compatible 与 Anthropic Adapter 忽略该字段。

## 错误处理

- Provider 设置尚未加载：不显示选择器，避免展示错误默认值。
- 保存中重复点击：忽略并禁用菜单项。
- PUT 失败：保留上一次成功配置，菜单保持可重试，错误显示在现有错误区域。
- 当前 Provider 切换为非 DeepSeek：自动关闭胶囊菜单并隐藏选择器。
- 未知 DeepSeek 模型：胶囊显示原始模型名；打开菜单后用户可切换到两个受支持模型之一。

## 验证

自动验证：

1. Provider 设置存储可往返保存 `thinking_enabled`。
2. 旧版设置文件缺少字段时默认启用思考模式。
3. Gateway GET/PUT 正确传递 `thinkingEnabled`，且不泄漏 API Key。
4. DeepSeek 非流式请求包含正确的 `thinking.type`。
5. DeepSeek 流式请求包含正确的 `thinking.type`。
6. Provider Runtime 的活跃 lease 不受全局切换影响。
7. TypeScript 构建通过。

界面验证：

1. 胶囊位于聊天框右下角、发送按钮左侧。
2. 四种模型与模式组合显示正确。
3. 菜单向上展开，不遮挡输入文字。
4. 点击外部与按 `Esc` 均可关闭。
5. 保存失败时 UI 不显示未成功的配置。
6. 窄屏下胶囊、引导/停止和发送控件不产生横向溢出。

## 验收标准

- 用户无需打开设置窗口即可切换两种 DeepSeek 模型和思考模式。
- 切换结果全局持久化，并真实改变 DeepSeek API 请求体。
- 正在运行的 Run 不被中途切换影响。
- 非 DeepSeek Provider 的现有行为保持不变。
- 自动测试、前端构建和界面检查全部通过。
