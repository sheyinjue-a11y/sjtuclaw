# CLI 与 Web 统一命令系统设计

## 目标

让 CLI 与 Web 使用同一套除 `/exit` 外的本地命令定义和执行逻辑。两端均提供 `/command` 命令清单；Web Composer 在输入 `/` 时提供可键盘操作的命令补全，并将非聊天命令结果显示为临时结果卡片。

## 范围与非目标

覆盖现有 `/session`、`/memory`、`/compact` 和 `/skill` 命令；保留 `/exit` 为 CLI 专属命令。命令结果不写入 Session 消息、Run 历史或长期记忆，刷新浏览器后消失。不改变 AgentLoop、Skill 快照、Memory 持久化格式或已有非命令聊天请求。

## 架构

新增 `sjtuclaw.runtime.commands`：

- `CommandCatalog` 是唯一命令定义源，包含命令名称、参数模板、说明、是否需要当前 Session，以及可供 Web 补全使用的元数据。
- `CommandService` 解析并执行命令，依赖既有 Session、Memory、Compaction 与 Skill 服务，返回 `CommandOutcome`。
- `CommandOutcome` 含人可读结果行，以及可选前端效果：刷新 Session/Memory/Skill、切换选中 Session、或提交一个带指定 Skill 的正常 Run。

CLI 通过 `CommandService` 处理本地命令并输出结果。Gateway 暴露命令目录和执行接口，在既有 Cookie 鉴权和同源校验之后调用同一服务。Web 从 Gateway 获取目录，在 Composer 中展示补全，并把执行 Outcome 显示成临时结果卡片。

## 命令契约

`/command` 自动列出以下命令及用法：

- `/session new|list|switch <id>|rename <id> <title>|delete <id>`
- `/memory list|add <content>|delete <id>`
- `/compact`
- `/skill list|usage|refresh|show <skill-name>|<skill-name> <task>`

`<skill-name>` 是当前 Skill 清单中的动态 ID，`presentation-outline` 仅为示例，不构成命名限制。`/skill show <skill-name>` 读取该 ID 的名称、版本、描述和可用状态。`/skill <skill-name> <task>` 返回“提交 Run”效果，由 Web 复用既有聊天提交路径，保证任务文本、Skill 引用和使用记录仍按当前 Run 合同持久化。其他命令不创建模型请求。

未知命令、参数不足、服务不可用、资源不存在或版本冲突均返回结构化失败 Outcome。CLI 显示错误文本；Web 显示错误样式结果卡片；两者都不得把失败命令发送给模型。

## Web 交互

当 Composer 的首字符为 `/` 时，显示按当前文本过滤的补全列表。每项展示命令、参数占位符与说明。`ArrowUp`/`ArrowDown` 改变选择，`Tab` 或 `Enter` 补全模板，补全后再次 `Enter` 执行；`Escape` 关闭。IME 合成期间不触发补全选择或提交。普通文本和带 `$skill` mention 的既有发送路径不变。

结果卡片仅存在当前前端运行期，显示命令、成功或失败状态和服务返回内容。Outcome 的刷新或切换效果完成后，前端刷新相应列表或选中相应 Session。

## Gateway 接口

- `GET /api/v1/commands`：返回可执行命令的补全目录，不含 CLI 专属 `/exit`。
- `POST /api/v1/sessions/{session_id}/commands`：接收命令文本，执行后返回 `CommandOutcome`。要求有效 Cookie、同源请求、存在且未归档的 Session。

接口不接受任意 shell 命令，不扩大 Tool 权限，且沿用现有 Gateway 的输入长度、错误响应和服务可用性约束。

## 验收与验证

- 单元测试覆盖目录、每个命令分支、参数错误、服务不可用和 Outcome 效果。
- CLI 测试覆盖 `/command` 与现有命令兼容性。
- Gateway 测试覆盖鉴权、同源、执行结果和 `/skill <name> <task>` 的正常 Run 提交。
- 前端测试覆盖补全筛选、键盘和 IME、结果卡片、错误显示和普通消息发送。
- 完成前运行完整非 live pytest、Ruff、mypy、前端生产构建及 `git diff --check`。
