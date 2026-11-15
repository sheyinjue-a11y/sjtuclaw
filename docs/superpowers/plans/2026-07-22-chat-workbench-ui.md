# SJTUClaw 聊天工作台 UI 实施计划

**Spec:** `docs/superpowers/specs/2026-07-22-chat-workbench-ui-design.md`

**Goal:** 在不改变 Agent 核心业务逻辑的前提下，完成可实时观测、结构清晰且滚动可靠的三栏聊天工作台。

**Architecture:** 增量扩展现有 Session 快照与 SSE 事件字段；React 将持久消息和当前 Run 临时事件合并为时间线；现有工作区、队列、附件和 Skill 服务保持不变，仅调整展示与交互。

**Global Constraints:** 仅修改相关 Gateway、React UI、样式和测试；保留现有 loopback/同源认证、权限、路径与审批规则；完整观测数据不得进入前端持久缓存或不安全 HTML；不增加依赖或虚构 ETA。

## Goal 1: Gateway 提供完整且受控的观测数据

**Spec Basis:** “Confirmed security boundary”、“Gateway contract”

**Outcome:** Session 快照和 SSE 能向已认证同源网页提供 Thinking、工具 Input/Output/Error 与队列名称所需字段，同时保留现有事件 ID、顺序、重放和认证行为。

**Scope:** 只扩展序列化与公开事件载荷；不修改消息持久化格式、Agent Loop、工具执行、权限决策或队列调度。

**Dependencies:** 无。

**Acceptance:** Gateway 集成测试证明新增字段正确、工具事件顺序不变、SSE 重放仍按稳定 ID 工作，并且未批准的 provider 私有字段仍不会意外透传。

**Checkpoint:** Gateway 数据契约及其自动测试可独立回滚。

## Goal 2: 对话区按真实时间流式呈现不同事件层级

**Spec Basis:** “Conversation timeline”、“Message presentation”

**Outcome:** 用户、Agent、Thinking、Final、系统事件和工具调用在同一时间线中明确区分；Assistant 增量实时出现；重连保留已有内容；工具 Accordion 实时创建和完成且默认折叠。

**Scope:** 使用 SSE `eventId` 去重并使用 `occurredAt` 计算可用耗时；终态刷新成功后再用 Session 快照替换临时事件；不改变服务端运行阶段和消息生成方式。

**Dependencies:** Goal 1。

**Acceptance:** 手工流式运行可观察到增量文本、光标、工具与系统事件顺序；模拟或实际重连不清空、不重复；持久消息不产生空白 SJTUClaw 卡片；失败工具明显并能从 `SCHEDULED_PERMISSION_DENIED` 跳转工作区。

**Checkpoint:** 完整消息时间线和工具 Accordion 可独立回滚。

## Goal 3: 输入区、队列和工作区交互符合指定结构

**Spec Basis:** “Composer and Skill display”、“Queue and workspace”

**Outcome:** Composer 只有一个“+”入口及两个指定菜单项；Skill 名称不显示 `$`；队列可展开；工作区以紧凑 Input Group 或已绑定路径状态呈现；错误可直达工作区。

**Scope:** 保留底层 `$skill-id` mention、附件上传、工作区绑定/替换/解绑和队列业务接口；不保留“刷新 Skill”菜单项，不显示不存在的 ETA。

**Dependencies:** Goal 1 提供队列文本，Goal 2 提供错误跳转入口。

**Acceptance:** 页面检查确认菜单根层只有“添加附件”和“添加 Skill”；Skill 选择仍提交原始 mention；队列行显示名称与真实状态；绑定前后工作区状态和“更换”入口可正常操作。

**Checkpoint:** Composer、队列与工作区交互可独立回滚。

## Goal 4: 三栏滚动和视觉层级稳定

**Spec Basis:** “Message presentation”、“Scrolling”

**Outcome:** 左栏、中间历史区和右栏分别纵向滚动；Composer 处于正常网格底部且不遮挡消息；自动跟随尊重用户主动上滚；滚动条细窄低干扰。

**Scope:** 延续现有配色和响应式抽屉结构，只调整相关布局、间距、气泡、Accordion、队列、工作区和滚动样式；不进行设计系统或目录重构。

**Dependencies:** Goal 2、Goal 3 的 DOM 结构稳定。

**Acceptance:** 桌面与响应式宽度下无横向溢出、遮挡或错误嵌套滚动；用户位于底部时新内容自动跟随，主动上滚后位置保持；三处滚动容器均可独立操作。

**Checkpoint:** UI 样式与滚动布局可独立回滚。

## Goal 5: 自动验证与实际页面验收完成

**Spec Basis:** “Verification”、“Non-goals”

**Outcome:** 修改通过现有 Python 测试与 TypeScript/Vite 构建，并在真实启动页面完成指定交互和布局检查。

**Scope:** 运行完整现有测试；不新增前端测试框架，不运行需要真实外部 LLM 凭据的可选 live 测试。

**Dependencies:** Goal 1–4。

**Acceptance:** 非 live Python 测试全部通过，`npm run build` 成功；实际页面检查覆盖菜单、流式状态、默认折叠、队列、工作区跳转、三栏滚动、遮挡、溢出与重复渲染，并记录未覆盖的真实 LLM 场景。

**Checkpoint:** 验证结果构成最终交付边界，不包含无关修复。
