# SJTUClaw Chat Workbench UI Design

## Goal

Improve the existing React chat workbench without changing Agent execution, permission, scheduling, attachment, Skill, or workspace business rules. The work covers streamed conversation rendering, the composer add menu, message hierarchy, tool observability, queue details, workspace binding presentation, and independent scrolling.

## Confirmed security boundary

The user approved a local full-observability UI. The authenticated loopback Gateway may expose persisted model reasoning, tool arguments, tool output, and tool errors to the same-origin browser. These values remain session-only UI data: the frontend must not copy them into `localStorage`, browser-side logs, or unsafe HTML. Thinking and tool details are collapsed by default, and existing workspace, permission, path, and origin checks remain unchanged.

## Gateway contract

- Session message payloads add `reasoningContent` and structured `toolCalls` containing call ID, tool name, and raw argument text.
- Pending request payloads add the stored request text and enough status data for a queue row. The UI uses the request text as the task name and shows the matching persisted Run status. No ETA is rendered because the current model does not provide one.
- Streamed `tool_call` events expose the existing call ID, tool name, and arguments.
- Streamed `tool_result` events expose the existing call ID, tool name, status, output content, and error code.
- Event IDs, occurrence timestamps, event order, bounded replay, authentication, and reconnect behavior remain unchanged.
- Existing SSE security tests are updated to continue rejecting unrelated provider-only fields while asserting the newly approved observability fields.

## Conversation timeline

- Persisted messages remain ordered by `sequenceNumber`.
- Active Run events are held only in React state, de-duplicated by SSE `eventId`, and appended in arrival order.
- Assistant deltas are accumulated into the current live Assistant block. A tool or system event closes that block so later deltas appear at their true later position.
- Tool calls create a running Accordion immediately. The matching result updates the same call with success or failure, output, error, and elapsed time when both client event timestamps are available.
- Reconnect changes only connection status. Existing live items and partial Assistant text remain in place. Browser `EventSource` continues using the server event ID for replay, while client de-duplication prevents repeated rendering.
- On terminal events, the latest Session snapshot becomes authoritative and temporary live items are cleared only after refresh succeeds.

## Message presentation

- User messages are right-aligned dark bubbles.
- Assistant messages are left-aligned light bubbles with one compact SJTUClaw avatar/identity row.
- Empty Assistant cards are not rendered.
- `reasoningContent` is rendered as a collapsed Thinking process section; `content` is rendered separately as Final response.
- System events use a compact neutral row.
- Tool calls use collapsed Accordions. The summary shows tool name and status, plus duration when known. Expanded content shows Input, Output, and Error. Failures use a red icon/background.
- `SCHEDULED_PERMISSION_DENIED` includes a “去绑定工作区” action that opens the details panel when needed, scrolls the workspace section into view, and focuses its input or change action.

## Composer and Skill display

- Remove the standalone attachment button and the “+ Skill” button.
- Keep one “+” add button.
- Its root menu contains exactly “添加附件” and “添加 Skill”, in that order.
- “添加 Skill” opens the existing Skill choices as a secondary view. “刷新 Skill” is removed.
- Skill labels omit the leading `$`. The existing raw `$skill-id` mention inserted into the composer and submitted to the backend is unchanged.

## Queue and workspace

- “队列中 N 项” is a button with collapsed/expanded state.
- Expanded rows show a concise name derived from stored request text and the matching Run status. ETA is absent until the backend provides real ETA data.
- The unbound workspace state uses one compact input group containing path input, directory picker, and bind action.
- The bound state shows the current path with a “更换” entry; existing unbind behavior remains available without dominating the section.
- Workspace-related tool errors use the same focus action as `SCHEDULED_PERMISSION_DENIED`.

## Scrolling

- The page remains a fixed-height three-column grid.
- Session navigation, message history, and details sidebar each own their vertical overflow.
- The composer remains the final normal grid row and never overlays message content.
- A history ref tracks whether the user is near the bottom. New messages and deltas scroll only while that condition remains true.
- All three scroll containers receive narrow, low-contrast visible scrollbar styling.

## Verification

- Extend Gateway integration tests for Session and SSE fields, ordering, and approved exposure.
- Run the complete existing Python test suite.
- Run the existing frontend TypeScript/Vite build. The project has no frontend test script.
- Start the Gateway and inspect the real page at desktop and responsive widths, checking overflow, composer overlap, duplicate rendering, menu contents, default Accordion state, queue expansion, workspace focus, independent scrolling, and streaming follow behavior.

## Non-goals

- No new Agent behavior, permission decision, scheduler semantics, persistence format, attachment processing, or Skill resolution behavior.
- No unrelated component extraction, directory move, dependency addition, design-system rewrite, or speculative ETA calculation.
