# SJTUClaw 中期报告

## 1. 已实现功能

- Step 0–5：本地配置、LLM 对话、多 Session、Prompt/Soul、长期记忆、上下文压缩和只读工具闭环。
- Step 6：loopback Gateway、一次性 bootstrap、React 工作台、SSE、附件、队列、停止/steer 与网页会话管理。
- Step 7：JSON 定时任务、一次性/周期计划、执行历史、失败重试与管理页。
- Step 8：工作区绑定、文件写入/补丁、附件复制、Shell、命令和下载；副作用需要单次审批。
- Step 9：本地 Skill 清单、内容寻址快照、刷新、手动指定、自动路由和使用记录。

近期迭代补充了 DeepSeek、OpenAI 兼容和 Anthropic Provider 适配与运行时切换；可热更新 Soul/System Prompt；PDF/PPT 轻量工作流；5 个内置 Skill；附件本轮选择清除、Skill 调用详情、轮次耗时和归档时残留排队 Run 取消。

## 2. 模块划分与核心数据

`models` 定义 Session、Message、Run、Task、Occurrence、Attachment、WorkspaceBinding、Approval、Memory、ProviderConfiguration 和 Skill；</br>`runtime` 负责 AgentLoop、队列、上下文、Provider 租约、记忆、权限、工作区、定时任务和 Skill 路由；</br>`ports` 与 `infrastructure` 隔离 Provider、JSON 存储、Windows 用户保护配置、Tokenizer 与工具；</br>`entrypoints`、`web` 提供 CLI、Gateway、SSE 与 React 页面。

完整文件映射见 [../project_structure.md](../project_structure.md)。

## 3. 后续计划与边界

浏览器视觉、响应式与无障碍仍需持续人工验收；真实 Provider/live 验收需由拥有相应 API 配额的操作者执行。后续目标是把 SJTUClaw 扩展为适配 SJTU 教学系统和 Canvas 的学习助手，继续完善定时任务、DDL 管理、学习计划、校园资讯，以及可独立安装的 SJTU Skill。
