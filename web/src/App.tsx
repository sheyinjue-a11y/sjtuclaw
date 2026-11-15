import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ChangeEvent,
  type MouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import {
  applySuggestion,
  commandSuggestions,
  moveSuggestion,
  type CommandDefinition,
} from "./commands";
import {
  completeTransientCommand,
  failTransientCommand,
  newTransientCommand,
  type TransientCommandItem,
} from "./commandTimeline";
import { MemoryDrawer } from "./MemoryDrawer";
import { formatRoundDuration } from "./roundTiming";
import { SchedulerView, type ApiClient } from "./SchedulerView";
import { manualSkillOptions } from "./skillMenu";
import {
  normalizeToolResources,
  visibleToolResources,
  type ToolResource,
} from "./toolResources";
import {
  chooseInitialSession,
  readActiveSession,
  writeActiveSession,
} from "./activeSession";

type DrawerName = "sessions" | "details";
type RunStatus = "queued" | "started" | "completed" | "failed" | "cancelled_before_start" | "cancelled" | "interrupted";
type ToolStatus = "running" | "success" | "error";
type ThemePreference = "light" | "dark" | "system";
type ProviderKind = "deepseek" | "openai_compatible" | "anthropic";
type MemoryHealth = "ready" | "unavailable" | "error";

type ProviderSettings = {
  provider: ProviderKind;
  model: string;
  baseUrl: string;
  thinkingEnabled: boolean;
  keySaved: boolean;
  tokenCounterMode: "deepseek" | "approximate";
};

type PromptSettings = {
  systemPrompt: {
    path: string;
    editable: false;
    restartRequired: boolean;
  };
  soul: {
    path: string;
    content: string;
  };
};

type ToolCallData = {
  callId: string;
  name: string;
  arguments: string;
};

type SessionSummary = {
  sessionId: string;
  title: string;
  archivedAt: string | null;
  archivedReason: string | null;
  createdAt: string;
  updatedAt: string;
};

type SessionMenuState = SessionSummary & {
  left: number;
  top: number;
};

type SessionDialogState = {
  kind: "rename" | "archive" | "purge";
  session: SessionSummary;
};

type SelectionRect = {
  left: number;
  top: number;
  right: number;
  bottom: number;
};

type SessionSelectionDrag = {
  pointerId: number;
  startClientX: number;
  startClientY: number;
  startLeft: number;
  startTop: number;
  active: boolean;
};

function normalizedRect(
  startX: number,
  startY: number,
  endX: number,
  endY: number,
): SelectionRect {
  return {
    left: Math.min(startX, endX),
    top: Math.min(startY, endY),
    right: Math.max(startX, endX),
    bottom: Math.max(startY, endY),
  };
}

function rectsIntersect(left: SelectionRect, right: SelectionRect): boolean {
  return left.left <= right.right
    && left.right >= right.left
    && left.top <= right.bottom
    && left.bottom >= right.top;
}

type Message = {
  messageId: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  createdAt: string;
  sequenceNumber: number;
  attachmentRefs: string[];
  inputKind: "user" | "steer";
  reasoningContent: string | null;
  toolCalls: ToolCallData[];
  toolCallId: string | null;
  toolName: string | null;
  toolResultStatus: "success" | "error" | null;
  toolErrorCode: string | null;
  structuredContent: unknown;
  resources: ToolResource[];
};

type PendingRequest = {
  requestId: string;
  acceptanceSequence: number;
  source: "cli" | "gateway" | "scheduler";
  text: string;
  submittedAt: string;
  attachmentRefs: string[];
  skillRefs: SkillReference[];
};

type Run = {
  runId: string;
  requestId: string;
  status: RunStatus;
  startedAt: string | null;
  completedAt: string | null;
  durationMs: number | null;
  assistantMessageId: string | null;
  selectedSkillRefs: SkillReference[];
};

type SkillReference = {
  skillId: string;
  snapshotId: string;
  snapshotHash: string;
  version: string;
};

type SkillManifest = {
  skillId: string;
  description: string | null;
  version: string | null;
  snapshotId: string | null;
  snapshotHash: string | null;
  available: boolean;
  unavailableReason: string | null;
};

type SkillUsage = {
  reference: SkillReference;
  source: "explicit" | "auto" | "scheduled_explicit";
  selectionOrder: number;
  reason: string;
  selectedAt: string;
  requestId: string;
  outputMessageId: string | null;
  resourcePaths: string[];
};

type CommandOutcome = {
  command: string;
  lines: string[];
  isError: boolean;
  effects: {
    refreshSessions: boolean;
    refreshMemories: boolean;
    refreshSkills: boolean;
    selectSessionId: string | null;
  };
};

type CommandResponse = {
  outcome: CommandOutcome;
  request?: Run;
};

type SessionDetail = SessionSummary & {
  summary: string;
  messages: Message[];
  pendingRequests: PendingRequest[];
  runs: Run[];
  workspace: WorkspaceBinding | null;
  pendingApproval: PendingApproval | null;
  skillUsages: SkillUsage[];
  tokenUsage: TokenUsage;
  contextWindow: ContextWindow | null;
};

type TokenUsage = {
  promptTokens: number;
  promptCacheHitTokens: number;
  promptCacheMissTokens: number;
  completionTokens: number;
  totalTokens: number;
  cacheBreakdownAvailable: boolean;
  trackingStartedAt: string | null;
};

type ContextWindow = {
  currentTokens: number;
  outputReserveTokens: number;
  limitTokens: number;
  compactionTriggerTokens: number;
  compactionTargetTokens: number;
  model: {
    modelId: string;
    displayName: string;
    contextWindowTokens: number;
  };
};

type WorkspaceBinding = {
  bindingId: string;
  displayPath: string;
  revision: number;
  boundAt: string;
};

type PendingApproval = {
  approvalId: string;
  runId: string;
  invocationId: string;
  toolName: string;
  safetyLevel: "read_only" | "write" | "execute";
  targets: string[];
  preview: string;
  reason: string;
  createdAt: string;
  expiresAt: string;
};

type Attachment = {
  attachmentId: string;
  sessionId: string;
  displayName: string;
  kind: "text" | "pdf";
  status: "uploading" | "processing" | "ready" | "failed";
  sizeBytes: number | null;
  normalizedCharCount: number | null;
  errorCode: string | null;
};

type SseEnvelope = {
  eventId: number;
  occurredAt: string;
  payload: Record<string, unknown>;
};

type LiveAssistantItem = {
  id: string;
  kind: "assistant";
  content: string;
  streaming: boolean;
};

type LiveToolItem = {
  id: string;
  kind: "tool";
  callId: string;
  name: string;
  input: string;
  output: string;
  errorCode: string | null;
  status: ToolStatus;
  startedAt: number | null;
  durationMs: number | null;
  structuredContent: unknown;
  resources: ToolResource[];
};

type LiveSkillItem = {
  id: string;
  kind: "skill";
  skillId: string;
  source: SkillUsage["source"];
  reason: string;
  output: string;
  status: ToolStatus;
  resourcePaths: string[];
};

type LiveSystemItem = {
  id: string;
  kind: "system";
  label: string;
};

type LiveTimelineItem = LiveAssistantItem | LiveToolItem | LiveSkillItem | LiveSystemItem;

const API_PREFIX = "/api/v1";
const focusable = 'button:not([disabled]), [href], textarea, input, [tabindex]:not([tabindex="-1"])';
const progressText: Record<string, string> = {
  "request.queued": "正在排队",
  "context.preparing": "正在整理上下文",
  "model.started": "正在理解问题",
  "tool.running": "正在调用工具",
  "model.synthesizing": "正在整理回答",
  "skill.selected": "已选择本地 Skill",
  "run.completed": "已完成",
  "run.failed": "运行失败",
  "run.cancelled": "已停止",
};

const runStatusText: Record<RunStatus, string> = {
  queued: "排队中",
  started: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled_before_start: "已取消",
  cancelled: "已停止",
  interrupted: "已中断",
};

const workspaceErrorCodes = new Set(["SCHEDULED_PERMISSION_DENIED", "WORKSPACE_NOT_CONFIGURED"]);
const providerDefaults: Record<ProviderKind, Pick<ProviderSettings, "model" | "baseUrl">> = {
  deepseek: { model: "deepseek-v4-flash", baseUrl: "https://api.deepseek.com" },
  openai_compatible: { model: "gpt-4.1-mini", baseUrl: "https://api.openai.com/v1" },
  anthropic: { model: "claude-sonnet-4-20250514", baseUrl: "https://api.anthropic.com" },
};

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

class ApiError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
  }
}

function Drawer({
  name,
  open,
  onClose,
  children,
}: {
  name: DrawerName;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const panel = useRef<HTMLElement>(null);
  const mediaQuery = name === "sessions" ? "(max-width: 799px)" : "(max-width: 1199px)";
  const [isDrawer, setIsDrawer] = useState(() => window.matchMedia(mediaQuery).matches);

  useEffect(() => {
    const media = window.matchMedia(mediaQuery);
    const sync = () => setIsDrawer(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, [mediaQuery]);

  useEffect(() => {
    if (!isDrawer || !open) return;
    const node = panel.current;
    node?.querySelector<HTMLElement>("button")?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key !== "Tab" || !node) return;
      const items = [...node.querySelectorAll<HTMLElement>(focusable)];
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [isDrawer, onClose, open]);

  return (
    <>
      {isDrawer && open && <button className="drawer-backdrop" type="button" onClick={onClose} aria-label="关闭抽屉" />}
      <aside
        ref={panel}
        id={`drawer-${name}`}
        className={`panel drawer drawer--${name} ${open ? "is-open" : ""}`}
        aria-label={name === "sessions" ? "会话" : "活动与附件"}
        aria-hidden={isDrawer && !open}
      >
        <button className="drawer__close" type="button" onClick={onClose} aria-label="关闭抽屉">×</button>
        {children}
      </aside>
    </>
  );
}

function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeSanitize]}
      urlTransform={safeUrl}
      components={{
        a: ({ href, children: linkChildren }) => <a href={href} target="_blank" rel="noreferrer">{linkChildren}</a>,
      }}
    >
      {children}
    </ReactMarkdown>
  );
}

function AssistantBubble({
  content,
  reasoning,
  streaming = false,
}: {
  content: string;
  reasoning?: string | null;
  streaming?: boolean;
}) {
  if (!content && !reasoning && !streaming) return null;
  return <article className={`message message--assistant ${streaming ? "message--partial" : ""}`}>
    <div className="message__identity"><span aria-hidden="true">S</span><strong>SJTUClaw</strong></div>
    {reasoning && <details className="thinking-block">
      <summary>Thinking process</summary>
      <div className="thinking-block__content"><Markdown>{reasoning}</Markdown></div>
    </details>}
    {(content || streaming) && <section className="final-response" aria-label="Final response">
      <p className="message__section-label">Final response</p>
      {content && <Markdown>{content}</Markdown>}
      {streaming && <span className="typing" aria-label="正在生成" />}
    </section>}
  </article>;
}

function SystemEvent({ label }: { label: string }) {
  return <div className="system-event"><span aria-hidden="true" />{label}</div>;
}

function ToolAccordion({
  name,
  input,
  output,
  errorCode,
  status,
  durationMs,
  resources,
  onWorkspace,
}: {
  name: string;
  input: string;
  output: string;
  errorCode: string | null;
  status: ToolStatus;
  durationMs: number | null;
  resources: ToolResource[];
  onWorkspace: () => void;
}) {
  const failed = status === "error";
  const statusLabel = status === "running" ? "运行中" : failed ? "失败" : "成功";
  const canBindWorkspace = errorCode !== null && workspaceErrorCodes.has(errorCode);
  return <details className={`tool-call ${failed ? "tool-call--error" : ""}`}>
    <summary>
      <span className="tool-call__status" aria-hidden="true">{failed ? "!" : status === "running" ? "·" : "✓"}</span>
      <strong>{name || "工具"}</strong>
      <span>{statusLabel}</span>
      {durationMs !== null && <time>{formatToolDuration(durationMs)}</time>}
    </summary>
    <div className="tool-call__details">
      <section><h3>Input</h3><pre>{formatToolContent(input, "无输入")}</pre></section>
      <section><h3>Output</h3><pre>{formatToolContent(output, status === "running" ? "等待输出" : "无输出")}</pre></section>
      <section><h3>Error</h3><pre>{errorCode ?? "无"}</pre></section>
      <div className="tool-call__actions">
        {visibleToolResources(resources, status).map((resource) => (
          <a key={`${resource.kind}:${resource.href}`} href={resource.href} download>
            下载 {resource.name}
          </a>
        ))}
        {canBindWorkspace && <button type="button" onClick={onWorkspace}>去绑定工作区</button>}
      </div>
    </div>
  </details>;
}

function SkillAccordion({
  skillId,
  source,
  reason,
  output,
  status,
  resourcePaths,
}: {
  skillId: string;
  source: SkillUsage["source"];
  reason: string;
  output: string;
  status: ToolStatus;
  resourcePaths: string[];
}) {
  const failed = status === "error";
  const statusLabel = status === "running" ? "运行中" : failed ? "失败" : "成功";
  return <details className={`tool-call skill-call ${failed ? "tool-call--error" : ""}`}>
    <summary>
      <span className="tool-call__status" aria-hidden="true">{failed ? "!" : status === "running" ? "·" : "S"}</span>
      <strong>{skillId || "Skill"}</strong>
      <span>{statusLabel}</span>
    </summary>
    <div className="tool-call__details">
      <section><h3>调用来源</h3><pre>{skillSourceText(source)}</pre></section>
      <section><h3>选择原因</h3><pre>{formatToolContent(reason, "未提供")}</pre></section>
      <section><h3>最终输出</h3><pre>{formatToolContent(output, status === "running" ? "等待最终输出" : "无最终输出")}</pre></section>
      {resourcePaths.length > 0 && <section><h3>已读 Skill 资源</h3><pre>{resourcePaths.join("\n")}</pre></section>}
    </div>
  </details>;
}

function TokenDashboards({ detail }: { detail: SessionDetail | null }) {
  const usage = detail?.tokenUsage;
  const context = detail?.contextWindow;
  const total = usage?.totalTokens ?? 0;
  const segmentWidth = (value: number) => total > 0 ? `${value / total * 100}%` : "0%";
  const ringStyle = context ? {
    background: contextRingBackground(context),
    "--compaction-threshold-angle": `${Math.min(context.compactionTriggerTokens / context.limitTokens, 1) * 360}deg`,
  } as CSSProperties : undefined;

  return <div className="token-dashboards" aria-label="Token 仪表盘">
    <div className="token-divider"><span>Token</span></div>
    <section className="token-card" aria-labelledby="token-usage-title">
      <p className="eyebrow" id="token-usage-title">当前窗口消耗</p>
      <p className="token-total">{formatTokens(total)}<small>tokens</small></p>
      <div className="token-stack" aria-hidden="true">
        {usage?.cacheBreakdownAvailable ? <>
          <i className="token-stack__hit" style={{ width: segmentWidth(usage.promptCacheHitTokens) }} />
          <i className="token-stack__miss" style={{ width: segmentWidth(usage.promptCacheMissTokens) }} />
        </> : <i className="token-stack__unknown" style={{ width: segmentWidth(usage?.promptTokens ?? 0) }} />}
        <i className="token-stack__output" style={{ width: segmentWidth(usage?.completionTokens ?? 0) }} />
      </div>
      <div className="token-legend">
        <span>缓存命中输入</span><strong>{usage?.cacheBreakdownAvailable ? formatTokens(usage.promptCacheHitTokens) : "—"}</strong>
        <span>缓存未命中输入</span><strong>{usage?.cacheBreakdownAvailable ? formatTokens(usage.promptCacheMissTokens) : "—"}</strong>
        <span>输出</span><strong>{formatTokens(usage?.completionTokens ?? 0)}</strong>
      </div>
    </section>
    <section className="token-card" aria-labelledby="context-window-title">
      <p className="eyebrow" id="context-window-title">上下文窗口</p>
      {context ? <div className="context-ring-block">
        <div
          className="context-ring"
          style={ringStyle}
          role="img"
          aria-label={`当前上下文 ${formatTokens(context.currentTokens)}，输出预留 ${formatTokens(context.outputReserveTokens)}，模型上限 ${formatTokens(context.limitTokens)}；红色刻度为 ${formatTokens(context.compactionTriggerTokens)} 自动压缩阈值`}
        >
          <div className="context-ring__center"><strong>{formatTokens(context.currentTokens)}</strong><small>of {formatTokens(context.limitTokens)}</small></div>
        </div>
        <div className="context-model" title={context.model.modelId}>{context.model.displayName}</div>
      </div> : <p className="muted">上下文数据不可用</p>}
    </section>
  </div>;
}

export default function App() {
  const [view, setView] = useState<"chat" | "scheduler">("chat");
  const [drawer, setDrawer] = useState<DrawerName | null>(null);
  const [collapsedPanels, setCollapsedPanels] = useState<Record<DrawerName, boolean>>({
    sessions: false,
    details: false,
  });
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [archivedSessions, setArchivedSessions] = useState<SessionSummary[]>([]);
  const [showArchivedSessions, setShowArchivedSessions] = useState(false);
  const [sessionQuery, setSessionQuery] = useState("");
  const [archivedSessionQuery, setArchivedSessionQuery] = useState("");
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [skills, setSkills] = useState<SkillManifest[]>([]);
  const [commandDefinitions, setCommandDefinitions] = useState<CommandDefinition[]>([]);
  const [commandTimeline, setCommandTimeline] = useState<
    Record<string, readonly TransientCommandItem[]>
  >({});
  const [commandIndex, setCommandIndex] = useState(0);
  const [commandCompletionDismissed, setCommandCompletionDismissed] = useState(false);
  const [selectedAttachmentIds, setSelectedAttachmentIds] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [liveItems, setLiveItems] = useState<LiveTimelineItem[]>([]);
  const [statusText, setStatusText] = useState("正在恢复会话");
  const [connectionStatus, setConnectionStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [approvalReason, setApprovalReason] = useState("");
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [decidedApprovalId, setDecidedApprovalId] = useState<string | null>(null);
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [addMenuView, setAddMenuView] = useState<"root" | "skills">("root");
  const [workspaceEditing, setWorkspaceEditing] = useState(false);
  const [sessionMenu, setSessionMenu] = useState<SessionMenuState | null>(null);
  const [sessionDialog, setSessionDialog] = useState<SessionDialogState | null>(null);
  const [renameTitle, setRenameTitle] = useState("");
  const [sessionMutationBusy, setSessionMutationBusy] = useState(false);
  const [bulkMode, setBulkMode] = useState(false);
  const [selectedSessionIds, setSelectedSessionIds] = useState<string[]>([]);
  const [selectionRect, setSelectionRect] = useState<SelectionRect | null>(null);
  const [bulkDeleteConfirm, setBulkDeleteConfirm] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [theme, setTheme] = useState<ThemePreference>(() => {
    const saved = window.localStorage.getItem("sjtuclaw.theme");
    return saved === "light" || saved === "dark" || saved === "system" ? saved : "system";
  });
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [providerDialogOpen, setProviderDialogOpen] = useState(false);
  const [providerSettings, setProviderSettings] = useState<ProviderSettings | null>(null);
  const [providerDraft, setProviderDraft] = useState<ProviderSettings | null>(null);
  const [providerApiKey, setProviderApiKey] = useState("");
  const [providerBusy, setProviderBusy] = useState(false);
  const [providerError, setProviderError] = useState<string | null>(null);
  const [providerQuickOpen, setProviderQuickOpen] = useState(false);
  const [providerQuickError, setProviderQuickError] = useState<string | null>(null);
  const [memoryDrawerOpen, setMemoryDrawerOpen] = useState(false);
  const [memoryHealth, setMemoryHealth] = useState<MemoryHealth>("ready");
  const [memoryRefreshToken, setMemoryRefreshToken] = useState(0);
  const [soulDialogOpen, setSoulDialogOpen] = useState(false);
  const [promptSettings, setPromptSettings] = useState<PromptSettings | null>(null);
  const [soulDraft, setSoulDraft] = useState("");
  const [soulBusy, setSoulBusy] = useState(false);
  const [soulError, setSoulError] = useState<string | null>(null);
  const triggers = useRef<Partial<Record<DrawerName, HTMLButtonElement>>>({});
  const fileInput = useRef<HTMLInputElement>(null);
  const textArea = useRef<HTMLTextAreaElement>(null);
  const history = useRef<HTMLDivElement>(null);
  const addMenu = useRef<HTMLDivElement>(null);
  const sessionMenuElement = useRef<HTMLDivElement>(null);
  const sessionNav = useRef<HTMLElement>(null);
  const sessionRows = useRef(new Map<string, HTMLButtonElement>());
  const sessionSelectionDrag = useRef<SessionSelectionDrag | null>(null);
  const eventSource = useRef<EventSource | null>(null);
  const selectedSession = useRef<string | null>(null);
  const activeSessionWrite = useRef(Promise.resolve());
  const seenEventIds = useRef(new Set<number>());
  const streamRunId = useRef<string | null>(null);
  const followHistory = useRef(true);
  const workspaceSection = useRef<HTMLElement>(null);
  const workspacePicker = useRef<HTMLButtonElement>(null);
  const workspaceChange = useRef<HTMLButtonElement>(null);
  const settingsMenu = useRef<HTMLDivElement>(null);
  const providerQuickMenu = useRef<HTMLDivElement>(null);
  const memoryTrigger = useRef<HTMLButtonElement>(null);

  const activeRun = detail?.runs.find((run) => run.status === "started") ?? null;
  const roundDurationByAssistantMessageId = new Map(
    (detail?.runs ?? []).flatMap((run) => (
      run.status === "completed"
        && run.assistantMessageId !== null
        && run.durationMs !== null
        ? [[run.assistantMessageId, run.durationMs] as const]
        : []
    )),
  );
  const queuedRequests = [...(detail?.pendingRequests ?? [])]
    .sort((left, right) => left.acceptanceSequence - right.acceptanceSequence);
  const rawCommandCompletions = commandSuggestions(commandDefinitions, skills, text);
  const commandCompletions = commandCompletionDismissed ? [] : rawCommandCompletions;

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.dataset.theme = theme === "system"
        ? (media.matches ? "dark" : "light")
        : theme;
      window.localStorage.setItem("sjtuclaw.theme", theme);
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);

  const close = () => {
    const previous = drawer;
    setDrawer(null);
    window.setTimeout(() => previous && triggers.current[previous]?.focus(), 0);
  };
  const open = (name: DrawerName, target: HTMLButtonElement) => {
    triggers.current[name] = target;
    setDrawer(name);
  };
  const isDrawerViewport = (name: DrawerName) => window.matchMedia(
    name === "sessions" ? "(max-width: 799px)" : "(max-width: 1199px)",
  ).matches;
  const panelExpanded = (name: DrawerName) => (
    isDrawerViewport(name) ? drawer === name : !collapsedPanels[name]
  );
  const togglePanel = (name: DrawerName, event: MouseEvent<HTMLButtonElement>) => {
    if (isDrawerViewport(name)) {
      if (drawer === name) close();
      else open(name, event.currentTarget);
      return;
    }
    setCollapsedPanels((current) => ({ ...current, [name]: !current[name] }));
  };

  const focusWorkspaceArea = () => {
    if (isDrawerViewport("details")) setDrawer("details");
    else setCollapsedPanels((current) => ({ ...current, details: false }));
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      workspaceSection.current?.scrollIntoView({ block: "center", behavior: "smooth" });
      (workspacePicker.current ?? workspaceChange.current)?.focus({ preventScroll: true });
    }));
  };

  const exitBulkMode = () => {
    if (bulkDeleting) return;
    setBulkMode(false);
    setSelectedSessionIds([]);
    setSelectionRect(null);
    setBulkDeleteConfirm(false);
    sessionSelectionDrag.current = null;
  };

  const toggleSessionSelection = (sessionId: string) => {
    setSelectedSessionIds((current) => current.includes(sessionId)
      ? current.filter((value) => value !== sessionId)
      : [...current, sessionId]);
  };

  const startSessionSelection = (event: ReactPointerEvent<HTMLElement>) => {
    if (!bulkMode || bulkDeleting || event.button !== 0 || event.pointerType !== "mouse") return;
    if ((event.target as HTMLElement).closest("button")) return;
    const node = sessionNav.current;
    if (!node) return;
    const bounds = node.getBoundingClientRect();
    sessionSelectionDrag.current = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startLeft: event.clientX - bounds.left + node.scrollLeft,
      startTop: event.clientY - bounds.top + node.scrollTop,
      active: false,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const moveSessionSelection = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = sessionSelectionDrag.current;
    const node = sessionNav.current;
    if (!drag || !node || drag.pointerId !== event.pointerId) return;
    if (!drag.active && Math.hypot(
      event.clientX - drag.startClientX,
      event.clientY - drag.startClientY,
    ) < 5) return;
    drag.active = true;
    const bounds = node.getBoundingClientRect();
    const localBox = normalizedRect(
      drag.startLeft,
      drag.startTop,
      event.clientX - bounds.left + node.scrollLeft,
      event.clientY - bounds.top + node.scrollTop,
    );
    const clientBox = normalizedRect(
      drag.startClientX,
      drag.startClientY,
      event.clientX,
      event.clientY,
    );
    const selected = sessions.flatMap((session) => {
      const row = sessionRows.current.get(session.sessionId);
      if (!row) return [];
      const rowBounds = row.getBoundingClientRect();
      return rectsIntersect(clientBox, {
        left: rowBounds.left,
        top: rowBounds.top,
        right: rowBounds.right,
        bottom: rowBounds.bottom,
      }) ? [session.sessionId] : [];
    });
    setSelectionRect(localBox);
    setSelectedSessionIds(selected);
    event.preventDefault();
  };

  const endSessionSelection = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = sessionSelectionDrag.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    sessionSelectionDrag.current = null;
    setSelectionRect(null);
  };

  const onHistoryScroll = () => {
    const node = history.current;
    if (!node) return;
    followHistory.current = node.scrollHeight - node.scrollTop - node.clientHeight < 72;
  };

  useEffect(() => {
    if (!followHistory.current) return;
    const frame = window.requestAnimationFrame(() => {
      const node = history.current;
      if (node) node.scrollTop = node.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [commandTimeline, detail?.messages, liveItems]);

  useEffect(() => {
    setCommandIndex((current) => rawCommandCompletions.length
      ? Math.min(Math.max(current, 0), rawCommandCompletions.length - 1)
      : 0);
  }, [commandDefinitions, rawCommandCompletions.length, skills, text]);

  useEffect(() => {
    if (!bulkMode) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !bulkDeleting) exitBulkMode();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [bulkMode, bulkDeleting]);

  useEffect(() => {
    if (!addMenuOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !addMenu.current?.contains(event.target)) {
        setAddMenuOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setAddMenuOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [addMenuOpen]);

  useEffect(() => {
    if (!settingsOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !settingsMenu.current?.contains(event.target)) {
        setSettingsOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSettingsOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [settingsOpen]);

  useEffect(() => {
    if (!providerQuickOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !providerQuickMenu.current?.contains(event.target)) {
        setProviderQuickOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setProviderQuickOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [providerQuickOpen]);

  useEffect(() => {
    if (providerSettings?.provider === "deepseek") return;
    setProviderQuickOpen(false);
    setProviderQuickError(null);
  }, [providerSettings?.provider]);

  useEffect(() => {
    if (!sessionMenu) return;
    window.requestAnimationFrame(() => {
      sessionMenuElement.current?.querySelector<HTMLButtonElement>("button")?.focus();
    });
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !sessionMenuElement.current?.contains(event.target)) {
        setSessionMenu(null);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSessionMenu(null);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [sessionMenu]);

  useEffect(() => {
    if (!sessionDialog || sessionMutationBusy) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSessionDialog(null);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [sessionDialog, sessionMutationBusy]);

  const refreshSession = async (sessionId: string): Promise<SessionDetail | undefined> => {
    const [sessionResponse, attachmentResponse] = await Promise.all([
      api<{ session: SessionDetail }>(`/sessions/${sessionId}`),
      api<{ attachments: Attachment[] }>(`/sessions/${sessionId}/attachments`),
    ]);
    if (selectedSession.current === sessionId) {
      setDetail(sessionResponse.session);
      setSessions((current) => current.map((session) => (
        session.sessionId === sessionId
          ? {
              ...session,
              title: sessionResponse.session.title,
              updatedAt: sessionResponse.session.updatedAt,
            }
          : session
      )));
      setAttachments(attachmentResponse.attachments);
      setDecidedApprovalId((current) => (
        current === sessionResponse.session.pendingApproval?.approvalId ? current : null
      ));
      return sessionResponse.session;
    }
    return undefined;
  };

  const selectSession = async (sessionId: string) => {
    eventSource.current?.close();
    eventSource.current = null;
    streamRunId.current = null;
    seenEventIds.current.clear();
    selectedSession.current = sessionId;
    setSelectedSessionId(sessionId);
    setSelectedAttachmentIds([]);
    setLiveItems([]);
    setConnectionStatus(null);
    setWorkspaceEditing(false);
    followHistory.current = true;
    setApprovalReason("");
    setDecidedApprovalId(null);
    try {
      const sessionDetail = await refreshSession(sessionId);
      if (selectedSession.current !== sessionId) return;
      const activeRun = sessionDetail?.runs.find((run) => run.status === "started");
      if (activeRun) subscribeToRun(sessionId, activeRun.runId);
      const write = activeSessionWrite.current.catch(() => undefined).then(async () => {
        if (selectedSession.current !== sessionId) return;
        await writeActiveSession(api, sessionId);
        if (selectedSession.current !== sessionId) return;
        window.localStorage.setItem("sjtuclaw.selected-session", sessionId);
      });
      activeSessionWrite.current = write;
      await write;
      if (selectedSession.current !== sessionId) return;
      setStatusText("等待输入");
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    }
  };

  const refreshSkills = async () => {
    const response = await api<{ skills: SkillManifest[] }>("/skills");
    setSkills(response.skills);
  };

  const refreshCommandDefinitions = async () => {
    const response = await api<{ commands: CommandDefinition[] }>("/commands");
    setCommandDefinitions(response.commands);
  };

  const refreshSessionLists = async () => {
    const [active, archived] = await Promise.all([
      api<{ sessions: SessionSummary[] }>("/sessions"),
      api<{ sessions: SessionSummary[] }>("/sessions?archived=true"),
    ]);
    setSessions(active.sessions);
    setArchivedSessions(archived.sessions);
    return active.sessions;
  };

  const refreshProviderSettings = async () => {
    const response = await api<{ provider: ProviderSettings }>("/settings/provider");
    setProviderSettings(response.provider);
    setProviderDraft(response.provider);
  };

  const refreshPromptSettings = async (): Promise<PromptSettings> => {
    const response = await api<PromptSettings>("/settings/prompts");
    setPromptSettings(response);
    return response;
  };

  const openSoulDialog = async () => {
    if (soulBusy) return;
    setSoulBusy(true);
    setSoulError(null);
    try {
      const settings = await refreshPromptSettings();
      setSoulDraft(settings.soul.content);
      setSoulDialogOpen(true);
    } catch (caught) {
      setSoulError(messageOf(caught));
      setSoulDialogOpen(true);
    } finally {
      setSoulBusy(false);
    }
  };

  const saveSoul = async () => {
    if (soulBusy) return;
    setSoulBusy(true);
    setSoulError(null);
    try {
      const response = await api<PromptSettings>("/settings/prompts/soul", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: soulDraft }),
      });
      setPromptSettings(response);
      setSoulDraft(response.soul.content);
      setSoulDialogOpen(false);
    } catch (caught) {
      setSoulError(messageOf(caught));
    } finally {
      setSoulBusy(false);
    }
  };

  const markMemoryUnavailable = useCallback(() => {
    setMemoryHealth("unavailable");
  }, []);

  const refreshMemoryHealth = useCallback(async () => {
    try {
      await api<{ items: unknown[]; nextCursor: string | null }>("/memories?limit=1");
      setMemoryHealth("ready");
    } catch (caught) {
      setMemoryHealth(caught instanceof ApiError && caught.code === "MEMORY_STORE_UNAVAILABLE"
        ? "unavailable"
        : "error");
    }
  }, []);

  const openProviderDialog = () => {
    setProviderDraft(providerSettings ?? {
      provider: "deepseek",
      ...providerDefaults.deepseek,
      thinkingEnabled: true,
      keySaved: false,
      tokenCounterMode: "deepseek",
    });
    setProviderApiKey("");
    setProviderError(null);
    setSettingsOpen(false);
    setProviderDialogOpen(true);
  };

  const saveProviderSettings = async () => {
    if (!providerDraft || providerBusy) return;
    setProviderBusy(true);
    try {
      const response = await api<{ provider: ProviderSettings }>("/settings/provider", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: providerDraft.provider,
          model: providerDraft.model,
          baseUrl: providerDraft.baseUrl,
          thinkingEnabled: providerDraft.thinkingEnabled,
          ...(providerApiKey.trim() ? { apiKey: providerApiKey.trim() } : {}),
        }),
      });
      setProviderSettings(response.provider);
      setProviderDraft(response.provider);
      setProviderApiKey("");
      setProviderDialogOpen(false);
      setProviderError(null);
    } catch (caught) {
      setProviderError(messageOf(caught));
    } finally {
      setProviderBusy(false);
    }
  };

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

  const insertSkillMention = (skillId: string) => {
    const input = textArea.current;
    const start = input?.selectionStart ?? text.length;
    const end = input?.selectionEnd ?? text.length;
    const mention = `$${skillId} `;
    setText(`${text.slice(0, start)}${mention}${text.slice(end)}`);
    setAddMenuOpen(false);
    setAddMenuView("root");
    window.requestAnimationFrame(() => {
      input?.focus();
      input?.setSelectionRange(start + mention.length, start + mention.length);
    });
  };

  useEffect(() => {
    let cancelled = false;
    const initialise = async () => {
      try {
        await exchangeBootstrapFragment();
        void refreshMemoryHealth();
        const [active, response, archivedResponse] = await Promise.all([
          readActiveSession(api),
          api<{ sessions: SessionSummary[] }>("/sessions"),
          api<{ sessions: SessionSummary[] }>("/sessions?archived=true"),
        ]);
        let list = response.sessions;
        let sessionId = chooseInitialSession(active.sessionId, list);
        if (sessionId === null) {
          const created = await api<{ session: SessionSummary }>("/sessions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: "新会话" }),
          });
          list = [created.session];
          sessionId = created.session.sessionId;
        }
        if (cancelled) return;
        setSessions(list);
        setArchivedSessions(archivedResponse.sessions);
        void refreshSkills().catch((caught: unknown) => setError(messageOf(caught)));
        void refreshCommandDefinitions().catch((caught: unknown) => setError(messageOf(caught)));
        void refreshProviderSettings().catch((caught: unknown) => setProviderError(messageOf(caught)));
        await selectSession(sessionId);
      } catch (caught) {
        if (!cancelled) {
          setError(messageOf(caught));
          setStatusText("无法连接本地 Gateway");
        }
      }
    };
    void initialise();
    return () => {
      cancelled = true;
      eventSource.current?.close();
    };
  }, [refreshMemoryHealth]);

  useEffect(() => {
    if (startedAt === null) return;
    const update = () => setElapsedMs(Math.max(0, Date.now() - startedAt));
    update();
    const timer = window.setInterval(update, 1_000);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  const createSession = async () => {
    try {
      const response = await api<{ session: SessionSummary }>("/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "新会话" }),
      });
      setSessions((current) => [response.session, ...current]);
      await selectSession(response.session.sessionId);
    } catch (caught) {
      setError(messageOf(caught));
    }
  };

  const toggleArchivedSessions = async () => {
    const next = !showArchivedSessions;
    if (!next) {
      setShowArchivedSessions(false);
      return;
    }
    try {
      const response = await api<{ sessions: SessionSummary[] }>("/sessions?archived=true");
      setArchivedSessions(response.sessions);
      setShowArchivedSessions(true);
      setBulkMode(false);
      setSelectedSessionIds([]);
      setSessionMenu(null);
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    }
  };

  const openSessionMenu = (session: SessionSummary, left: number, top: number) => {
    setSessionMenu({
      ...session,
      left: Math.min(Math.max(8, left), Math.max(8, window.innerWidth - 184)),
      top: Math.min(Math.max(8, top), Math.max(8, window.innerHeight - 104)),
    });
  };

  const openSessionDialog = (kind: SessionDialogState["kind"]) => {
    if (!sessionMenu) return;
    setSessionDialog({ kind, session: sessionMenu });
    setRenameTitle(sessionMenu.title);
    setSessionMenu(null);
  };

  const renameSession = async () => {
    if (sessionDialog?.kind !== "rename") return;
    const title = renameTitle.trim();
    if (!title || title === sessionDialog.session.title) {
      setSessionDialog(null);
      return;
    }
    setSessionMutationBusy(true);
    try {
      const response = await api<{ session: SessionSummary }>(`/sessions/${sessionDialog.session.sessionId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      setSessions((current) => current.map((session) => (
        session.sessionId === response.session.sessionId ? response.session : session
      )));
      if (selectedSession.current === response.session.sessionId) {
        setDetail((current) => current ? { ...current, ...response.session } : current);
      }
      setSessionDialog(null);
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setSessionMutationBusy(false);
    }
  };

  const archiveSession = async () => {
    if (sessionDialog?.kind !== "archive") return;
    const archivedId = sessionDialog.session.sessionId;
    setSessionMutationBusy(true);
    try {
      const response = await api<{ session: SessionSummary }>(`/sessions/${archivedId}/archive`, { method: "POST" });
      setSessionDialog(null);
      setArchivedSessions((current) => [response.session, ...current.filter((session) => session.sessionId !== archivedId)]);
      const remaining = sessions.filter((session) => session.sessionId !== archivedId);
      setSessions(remaining);
      if (selectedSession.current === archivedId) {
        eventSource.current?.close();
        selectedSession.current = null;
        setSelectedSessionId(null);
        setDetail(null);
        setAttachments([]);
        window.localStorage.removeItem("sjtuclaw.selected-session");
        if (remaining.length > 0) {
          await selectSession(remaining[0].sessionId);
        } else {
          const response = await api<{ session: SessionSummary }>("/sessions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: "新会话" }),
          });
          setSessions([response.session]);
          await selectSession(response.session.sessionId);
        }
      }
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setSessionMutationBusy(false);
    }
  };

  const restoreSession = async (session: SessionSummary) => {
    setSessionMutationBusy(true);
    try {
      const response = await api<{ session: SessionSummary }>(`/sessions/${session.sessionId}/restore`, { method: "POST" });
      setSessionMenu(null);
      setArchivedSessions((current) => current.filter((item) => item.sessionId !== session.sessionId));
      setSessions((current) => [response.session, ...current.filter((item) => item.sessionId !== session.sessionId)]);
      setShowArchivedSessions(false);
      await selectSession(response.session.sessionId);
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setSessionMutationBusy(false);
    }
  };

  const deleteSession = async () => {
    if (sessionDialog?.kind !== "purge") return;
    const sessionId = sessionDialog.session.sessionId;
    setSessionMutationBusy(true);
    try {
      await api<void>(`/sessions/${sessionId}`, { method: "DELETE" });
      setArchivedSessions((current) => current.filter((item) => item.sessionId !== sessionId));
      setSessionDialog(null);
      if (selectedSession.current === sessionId) {
        selectedSession.current = null;
        setSelectedSessionId(null);
        setDetail(null);
      }
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setSessionMutationBusy(false);
    }
  };

  const deleteSelectedSessions = async () => {
    const ids = [...selectedSessionIds];
    if (!ids.length || bulkDeleting) return;
    setBulkDeleting(true);
    try {
      const results = await Promise.allSettled(ids.map((sessionId) => (
        api<void>(`/sessions/${sessionId}/archive`, { method: "POST" })
      )));
      const deletedIds = new Set(
        results.flatMap((result, index) => result.status === "fulfilled" ? [ids[index]] : []),
      );
      const failedIds = ids.filter((sessionId) => !deletedIds.has(sessionId));
      const remaining = sessions.filter((session) => !deletedIds.has(session.sessionId));
      setSessions(remaining);
      setSelectedSessionIds(failedIds);
      setBulkDeleteConfirm(false);

      if (selectedSession.current && deletedIds.has(selectedSession.current)) {
        eventSource.current?.close();
        selectedSession.current = null;
        setSelectedSessionId(null);
        setDetail(null);
        setAttachments([]);
        setLiveItems([]);
        window.localStorage.removeItem("sjtuclaw.selected-session");
        if (remaining.length > 0) {
          await selectSession(remaining[0].sessionId);
        } else {
          const response = await api<{ session: SessionSummary }>("/sessions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: "新会话" }),
          });
          setSessions([response.session]);
          await selectSession(response.session.sessionId);
        }
      }

      if (failedIds.length > 0) {
        const firstFailure = results.find((result) => result.status === "rejected");
        setError(`有 ${failedIds.length} 个会话删除失败：${
          firstFailure?.status === "rejected" ? messageOf(firstFailure.reason) : "请重试"
        }`);
      } else {
        setBulkMode(false);
        setSelectedSessionIds([]);
        setError(null);
      }
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setBulkDeleting(false);
    }
  };

  const uploadFiles = async (event: ChangeEvent<HTMLInputElement>) => {
    const sessionId = selectedSessionId;
    const files = [...(event.target.files ?? [])];
    event.target.value = "";
    if (!sessionId || !files.length) return;
    setBusy(true);
    try {
      const uploaded = await Promise.all(files.map(async (file) => {
        const response = await api<{ attachment: Attachment }>(`/sessions/${sessionId}/attachments`, {
          method: "POST",
          headers: {
            "Content-Type": uploadContentType(file),
            "X-Attachment-Name": file.name,
          },
          body: file,
        });
        return response.attachment;
      }));
      if (selectedSession.current === sessionId) {
        setAttachments((current) => [...current, ...uploaded]);
        setSelectedAttachmentIds((current) => [
          ...current,
          ...uploaded.filter((attachment) => attachment.status === "ready").map((attachment) => attachment.attachmentId),
        ]);
      }
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setBusy(false);
    }
  };

  const executeCommand = async (sessionId: string, command: string) => {
    const anchorId = crypto.randomUUID();
    setCommandTimeline((current) => ({
      ...current,
      [sessionId]: [
        ...(current[sessionId] ?? []),
        newTransientCommand(anchorId, command),
      ],
    }));
    setBusy(true);
    try {
      const response = await api<CommandResponse>(`/sessions/${sessionId}/commands`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command, idempotencyKey: crypto.randomUUID() }),
      });
      const targetSessionId = response.outcome.effects.selectSessionId ?? sessionId;
      setCommandTimeline((current) => completeTransientCommand(
        current,
        sessionId,
        targetSessionId,
        anchorId,
        {
          command: response.outcome.command,
          lines: response.outcome.lines,
          isError: response.outcome.isError,
        },
      ));
      setText("");
      setSelectedAttachmentIds([]);
      setCommandCompletionDismissed(false);

      if (response.outcome.effects.refreshSessions) await refreshSessionLists();
      if (response.outcome.effects.refreshMemories) setMemoryRefreshToken((current) => current + 1);
      if (response.outcome.effects.refreshSkills) await refreshSkills();

      if (response.outcome.effects.selectSessionId) {
        await selectSession(response.outcome.effects.selectSessionId);
      } else if (response.request) {
        setStartedAt(Date.now());
        setElapsedMs(0);
        await refreshSession(sessionId);
        subscribeToRun(sessionId, response.request.runId);
      }
      setStatusText(response.outcome.isError ? "命令执行失败" : response.request ? "正在排队" : "命令已完成");
      setError(null);
    } catch (caught) {
      const message = messageOf(caught);
      setCommandTimeline((current) => failTransientCommand(
        current,
        sessionId,
        anchorId,
        command,
        message,
      ));
      setStartedAt(null);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const sessionId = selectedSessionId;
    const cleanText = text.trim();
    if (!sessionId || !cleanText || busy || detail?.archivedAt) return;
    const selectedAttachments = attachments.filter((attachment) => selectedAttachmentIds.includes(attachment.attachmentId));
    if (selectedAttachments.some((attachment) => attachment.status !== "ready")) {
      setError("请先移除尚未就绪的附件。");
      return;
    }
    if (cleanText.startsWith("/")) {
      await executeCommand(sessionId, cleanText);
      return;
    }
    setBusy(true);
    setStartedAt(Date.now());
    setElapsedMs(0);
    try {
      const response = await api<{ request: Run }>(`/sessions/${sessionId}/requests`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: cleanText,
          attachmentRefs: selectedAttachmentIds,
          idempotencyKey: crypto.randomUUID(),
        }),
      });
      setText("");
      setSelectedAttachmentIds([]);
      setStatusText(response.request.status === "queued" ? "正在排队" : "正在运行");
      setError(null);
      await refreshSession(sessionId);
      subscribeToRun(sessionId, response.request.runId);
    } catch (caught) {
      setStartedAt(null);
      setError(messageOf(caught));
    } finally {
      setBusy(false);
    }
  };

  const steer = async () => {
    if (!selectedSessionId || !activeRun || !text.trim()) return;
    setBusy(true);
    try {
      await api(`/sessions/${selectedSessionId}/runs/${activeRun.runId}/steer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          steerId: crypto.randomUUID(),
          idempotencyKey: crypto.randomUUID(),
          text: text.trim(),
        }),
      });
      setText("");
      setStatusText("正在应用引导");
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setBusy(false);
    }
  };

  const cancelQueuedRequest = async (requestId: string) => {
    const sessionId = selectedSessionId;
    if (!sessionId || busy || isArchivedSession) return;
    setBusy(true);
    try {
      await api(`/sessions/${sessionId}/requests/${requestId}`, { method: "DELETE" });
      await refreshSession(sessionId);
      setStatusText("已撤回排队请求");
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setBusy(false);
    }
  };

  const steerQueuedRequest = async (request: PendingRequest) => {
    const sessionId = selectedSessionId;
    if (!sessionId || !activeRun || busy || isArchivedSession || request.attachmentRefs.length || request.skillRefs.length) return;
    setBusy(true);
    try {
      await api(`/sessions/${sessionId}/runs/${activeRun.runId}/queued-requests/${request.requestId}/steer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          steerId: crypto.randomUUID(),
          idempotencyKey: crypto.randomUUID(),
        }),
      });
      await refreshSession(sessionId);
      setStatusText("排队请求已改为引导");
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    if (!selectedSessionId || !activeRun || isArchivedSession) return;
    try {
      await api(`/sessions/${selectedSessionId}/runs/${activeRun.runId}/stop`, { method: "POST" });
      setStatusText("正在停止运行");
    } catch (caught) {
      setError(messageOf(caught));
    }
  };

  const pickWorkspace = async () => {
    const sessionId = selectedSessionId;
    if (!sessionId || workspaceBusy || isArchivedSession) return;
    setWorkspaceBusy(true);
    try {
      const response = await api<{ path: string | null }>(`/sessions/${sessionId}/workspace/pick`, {
        method: "POST",
      });
      if (!response.path) return;
      await api<{ workspace: WorkspaceBinding }>(`/sessions/${sessionId}/workspace`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root: response.path }),
      });
      setWorkspaceEditing(false);
      await refreshSession(sessionId);
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setWorkspaceBusy(false);
    }
  };

  const unbindWorkspace = async () => {
    const sessionId = selectedSessionId;
    if (!sessionId || workspaceBusy || isArchivedSession) return;
    setWorkspaceBusy(true);
    try {
      await api<void>(`/sessions/${sessionId}/workspace`, { method: "DELETE" });
      setWorkspaceEditing(false);
      await refreshSession(sessionId);
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setWorkspaceBusy(false);
    }
  };

  const decideApproval = async (decision: "approve" | "deny") => {
    const sessionId = selectedSessionId;
    const approval = detail?.pendingApproval;
    if (!sessionId || !approval || approvalBusy || isArchivedSession) return;
    const reason = approvalReason.trim();
    setApprovalBusy(true);
    try {
      await api(`/sessions/${sessionId}/approvals/${approval.approvalId}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          decision,
          idempotencyKey: crypto.randomUUID(),
          ...(decision === "deny" && reason ? { reason } : {}),
        }),
      });
      setApprovalReason("");
      setDecidedApprovalId(approval.approvalId);
      setStatusText("审批已提交，等待继续运行");
      await refreshSession(sessionId);
      subscribeToRun(sessionId, approval.runId);
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setApprovalBusy(false);
    }
  };

  const subscribeToRun = (sessionId: string, runId: string) => {
    eventSource.current?.close();
    if (streamRunId.current !== runId) {
      setLiveItems([]);
      seenEventIds.current.clear();
    }
    streamRunId.current = runId;
    followHistory.current = true;
    setConnectionStatus("正在连接流式输出");
    const source = new EventSource(`${API_PREFIX}/sessions/${sessionId}/runs/${runId}/events`);
    eventSource.current = source;
    const read = (event: Event) => JSON.parse((event as MessageEvent<string>).data) as SseEnvelope;
    const accept = (event: Event) => {
      if (selectedSession.current !== sessionId) return null;
      const envelope = read(event);
      if (envelope.eventId > 0) {
        if (seenEventIds.current.has(envelope.eventId)) return null;
        seenEventIds.current.add(envelope.eventId);
      }
      return envelope;
    };
    const appendSystemEvent = (envelope: SseEnvelope, label: string) => {
      setLiveItems((current) => [
        ...finishStreamingItem(current),
        { id: `system-${envelope.eventId}-${label}`, kind: "system", label },
      ]);
    };
    source.onopen = () => {
      if (selectedSession.current === sessionId) setConnectionStatus(null);
    };
    source.addEventListener("snapshot", (event) => {
      const payload = read(event).payload;
      const snapshot = payload.session;
      if (snapshot && typeof snapshot === "object" && selectedSession.current === sessionId) {
        const run = payload.run as Run | undefined;
        const terminal = run && !["queued", "started"].includes(run.status);
        if (terminal) {
          setDetail(snapshot as SessionDetail);
          setLiveItems([]);
          setStatusText(runStatusText[run.status]);
          setStartedAt(null);
          if (run.durationMs !== null) setElapsedMs(run.durationMs);
          setConnectionStatus(null);
          streamRunId.current = null;
          source.close();
          if (eventSource.current === source) eventSource.current = null;
          return;
        }
        setDetail((current) => current ?? snapshot as SessionDetail);
      }
    });
    source.addEventListener("run_progress", (event) => {
      const envelope = accept(event);
      if (!envelope) return;
      const code = envelope.payload.code;
      if (typeof code === "string") {
        if (code === "skill.selected") {
          const skillId = envelope.payload.skillId;
          const sourceKind = envelope.payload.source;
          const reason = envelope.payload.reason;
          if (typeof skillId === "string" && skillId) {
            setStatusText(progressText[code]);
            setLiveItems((current) => [
              ...finishStreamingItem(current),
              {
                id: `skill-${envelope.eventId}`,
                kind: "skill",
                skillId,
                source: sourceKind === "auto" || sourceKind === "scheduled_explicit"
                  ? sourceKind
                  : "explicit",
                reason: typeof reason === "string" ? reason : "",
                output: "",
                status: "running",
                resourcePaths: [],
              },
            ]);
            return;
          }
        }
        if (code === "approval_required") {
          void refreshSession(sessionId);
        }
        const label = progressText[code] ?? code;
        setStatusText(label);
        appendSystemEvent(envelope, label);
      }
    });
    source.addEventListener("assistant_delta", (event) => {
      const envelope = accept(event);
      if (!envelope) return;
      const delta = envelope.payload.delta;
      if (typeof delta !== "string" || !delta) return;
      setLiveItems((current) => {
        const last = current.at(-1);
        if (last?.kind === "assistant" && last.streaming) {
          return [...current.slice(0, -1), { ...last, content: last.content + delta }];
        }
        return [...current, {
          id: `assistant-${envelope.eventId}`,
          kind: "assistant",
          content: delta,
          streaming: true,
        }];
      });
    });
    source.addEventListener("assistant_message", (event) => {
      const envelope = accept(event);
      if (!envelope) return;
      const content = envelope.payload.content;
      if (typeof content !== "string") return;
      setLiveItems((current) => {
        const withCompletedSkills = current.map((item) => item.kind === "skill" && item.status === "running"
          ? { ...item, output: content, status: "success" as const }
          : item);
        const index = findLastItemIndex(withCompletedSkills, (item) => item.kind === "assistant" && item.streaming);
        if (index < 0) {
          return content ? [...withCompletedSkills, {
            id: `assistant-${envelope.eventId}`,
            kind: "assistant",
            content,
            streaming: false,
          }] : withCompletedSkills;
        }
        return withCompletedSkills.map((item, itemIndex) => itemIndex === index && item.kind === "assistant"
          ? { ...item, content, streaming: false }
          : item);
      });
    });
    source.addEventListener("tool_call", (event) => {
      const envelope = accept(event);
      if (!envelope) return;
      const callId = envelope.payload.callId;
      const name = envelope.payload.name;
      const input = envelope.payload.input;
      if (typeof callId !== "string" || typeof name !== "string") return;
      setLiveItems((current) => [
        ...finishStreamingItem(current),
        {
          id: `tool-${callId}`,
          kind: "tool",
          callId,
          name,
          input: typeof input === "string" ? input : "",
          output: "",
          errorCode: null,
          status: "running",
          startedAt: parseEventTime(envelope.occurredAt),
          durationMs: null,
          structuredContent: null,
          resources: [],
        },
      ]);
    });
    source.addEventListener("tool_result", (event) => {
      const envelope = accept(event);
      if (!envelope) return;
      const callId = envelope.payload.callId;
      const name = envelope.payload.tool;
      if (typeof callId !== "string" || typeof name !== "string") return;
      const status = envelope.payload.status === "error" ? "error" : "success";
      const output = typeof envelope.payload.output === "string" ? envelope.payload.output : "";
      const errorCode = typeof envelope.payload.errorCode === "string" ? envelope.payload.errorCode : null;
      const structuredContent = envelope.payload.structuredContent ?? null;
      const resources = normalizeToolResources(envelope.payload.resources);
      const completedAt = parseEventTime(envelope.occurredAt);
      setLiveItems((current) => {
        const index = findLastItemIndex(current, (item) => item.kind === "tool" && item.callId === callId);
        if (index < 0) return [
          ...finishStreamingItem(current),
          {
            id: `tool-${callId}`,
            kind: "tool",
            callId,
            name,
            input: "",
            output,
            errorCode,
            status,
            startedAt: null,
            durationMs: null,
            structuredContent,
            resources,
          },
        ];
        return current.map((item, itemIndex) => {
          if (itemIndex !== index || item.kind !== "tool") return item;
          return {
            ...item,
            name,
            output,
            errorCode,
            status,
            structuredContent,
            resources,
            durationMs: item.startedAt === null || completedAt === null
              ? null
              : Math.max(0, completedAt - item.startedAt),
          };
        });
      });
    });
    const systemEvents: Record<string, string> = {
      request_accepted: "请求已接受",
      request_queued: "请求已进入队列",
      run_started: "开始生成",
      approval_required: "等待本次审批",
      approval_resumed: "审批已决定，正在恢复运行",
      compaction_completed: "上下文压缩完成",
      compaction_failed: "上下文压缩失败",
      steer_accepted: "引导已接受",
      steer_waiting_safe_point: "引导正在等待安全点",
      steer_applied: "引导已应用",
      steer_rejected: "引导被拒绝",
    };
    Object.entries(systemEvents).forEach(([eventType, fallback]) => {
      source.addEventListener(eventType, (event) => {
        const envelope = accept(event);
        if (!envelope) return;
        const code = envelope.payload.code;
        const label = typeof code === "string" && code ? progressText[code] ?? code : fallback;
        appendSystemEvent(envelope, label);
        setStatusText(label);
        if (["approval_required", "approval_resumed"].includes(eventType)) {
          void refreshSession(sessionId);
        }
      });
    });
    source.addEventListener("replay_window_expired", (event) => {
      if (seenEventIds.current.has(0)) return;
      seenEventIds.current.add(0);
      const envelope = read(event);
      appendSystemEvent(envelope, "事件重放窗口已过期，已同步会话");
      void refreshSession(sessionId);
    });
    const finish = (event: Event) => {
      const envelope = accept(event);
      if (!envelope) return;
      const payload = envelope.payload;
      const duration = payload.durationMs;
      if (typeof duration === "number") setElapsedMs(duration);
      setStartedAt(null);
      setStatusText(event.type === "run_completed" ? "已完成" : "运行已结束");
      setConnectionStatus(null);
      source.close();
      if (eventSource.current === source) eventSource.current = null;
      if (selectedSession.current === sessionId) {
        void refreshSession(sessionId).then(() => {
          if (selectedSession.current !== sessionId || streamRunId.current !== runId) return;
          setLiveItems([]);
          streamRunId.current = null;
          seenEventIds.current.clear();
        }).catch((caught: unknown) => setError(messageOf(caught)));
      }
    };
    source.addEventListener("run_completed", finish);
    source.addEventListener("run_failed", finish);
    source.addEventListener("run_cancelled", finish);
    source.onerror = () => {
      if (selectedSession.current === sessionId && streamRunId.current === runId) {
        setConnectionStatus("流式连接正在重连");
      }
    };
  };

  const toggleAttachment = (attachmentId: string) => {
    setSelectedAttachmentIds((current) => current.includes(attachmentId)
      ? current.filter((value) => value !== attachmentId)
      : [...current, attachmentId]);
  };

  const toolCallsById = new Map<string, ToolCallData>();
  const toolResultsByCallId = new Map<string, Message>();
  const skillUsagesByOutputMessageId = new Map<string, SkillUsage[]>();
  detail?.messages.forEach((message) => {
    message.toolCalls.forEach((call) => toolCallsById.set(call.callId, call));
    if (message.role === "tool" && message.toolCallId) toolResultsByCallId.set(message.toolCallId, message);
  });
  detail?.skillUsages.forEach((usage) => {
    if (!usage.outputMessageId) return;
    const usages = skillUsagesByOutputMessageId.get(usage.outputMessageId) ?? [];
    skillUsagesByOutputMessageId.set(usage.outputMessageId, [...usages, usage]);
  });
  const liveToolCallIds = new Set(liveItems.flatMap((item) => item.kind === "tool" ? [item.callId] : []));
  const activeSessionQuery = (showArchivedSessions ? archivedSessionQuery : sessionQuery)
    .trim()
    .toLocaleLowerCase();
  const displayedSessions = (showArchivedSessions ? archivedSessions : sessions)
    .filter((session) => session.title.toLocaleLowerCase().includes(activeSessionQuery));
  const isArchivedSession = detail?.archivedAt !== null && detail?.archivedAt !== undefined;

  return <main className={`workbench ${collapsedPanels.sessions ? "workbench--sessions-collapsed" : ""} ${collapsedPanels.details ? "workbench--details-collapsed" : ""}`}>
    <Drawer name="sessions" open={drawer === "sessions"} onClose={close}>
      <div className="brand"><span className="brand__mark">S</span><span>SJTUClaw</span></div>
      <button className="new-session" type="button" onClick={() => void createSession()} disabled={showArchivedSessions || bulkMode || bulkDeleting}>+ 新建 Session</button>
      <nav
        className={`session-nav ${bulkMode ? "session-nav--bulk" : ""}`}
        aria-label="工作区导航"
        ref={sessionNav}
        onPointerDown={startSessionSelection}
        onPointerMove={moveSessionSelection}
        onPointerUp={endSessionSelection}
        onPointerCancel={endSessionSelection}
      >
        <div className="session-nav__heading">
          <p className="eyebrow">{showArchivedSessions ? "已归档会话" : "最近会话"}</p>
          {!showArchivedSessions && !bulkMode && <button type="button" onClick={() => {
            setBulkMode(true);
            setSessionQuery("");
            setSessionMenu(null);
          }}>批量管理</button>}
        </div>
        {!showArchivedSessions && !bulkMode && <input
          className="session-search"
          value={sessionQuery}
          onChange={(event) => setSessionQuery(event.target.value)}
          placeholder="搜索会话名称"
          aria-label="搜索会话名称"
        />}
        {showArchivedSessions && <input
          className="session-search"
          value={archivedSessionQuery}
          onChange={(event) => setArchivedSessionQuery(event.target.value)}
          placeholder="搜索已归档会话"
          aria-label="搜索已归档会话"
        />}
        {bulkMode && <div className="session-bulk-toolbar" aria-label="批量会话操作">
          <span>已选 {selectedSessionIds.length} 项</span>
          <button type="button" onClick={() => setSelectedSessionIds(sessions.map((session) => session.sessionId))} disabled={bulkDeleting || selectedSessionIds.length === sessions.length}>全选</button>
          <button className="session-bulk-toolbar__delete" type="button" onClick={() => setBulkDeleteConfirm(true)} disabled={bulkDeleting || selectedSessionIds.length === 0}>批量归档</button>
          <button type="button" onClick={exitBulkMode} disabled={bulkDeleting}>取消</button>
        </div>}
        {displayedSessions.length === 0 && <p className="session-nav__empty">{activeSessionQuery ? "未找到匹配的会话" : "暂无会话"}</p>}
        {displayedSessions.map((session) => {
          const bulkSelected = selectedSessionIds.includes(session.sessionId);
          return <button
          className={`session ${session.sessionId === selectedSessionId ? "session--active" : ""} ${bulkSelected ? "session--bulk-selected" : ""}`}
          type="button"
          key={session.sessionId}
          ref={(node) => {
            if (node) sessionRows.current.set(session.sessionId, node);
            else sessionRows.current.delete(session.sessionId);
          }}
          aria-haspopup={bulkMode ? undefined : "menu"}
          aria-pressed={bulkMode ? bulkSelected : undefined}
          onClick={() => {
            if (bulkMode) toggleSessionSelection(session.sessionId);
            else void selectSession(session.sessionId);
          }}
          onContextMenu={(event) => {
            event.preventDefault();
            if (bulkMode) return;
            event.currentTarget.focus();
            openSessionMenu(session, event.clientX, event.clientY);
          }}
          onKeyDown={(event) => {
            if (bulkMode) return;
            if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
            event.preventDefault();
            const bounds = event.currentTarget.getBoundingClientRect();
            openSessionMenu(session, bounds.left + 20, bounds.bottom - 4);
          }}
        >
          {bulkMode && <span className="session__check" aria-hidden="true">{bulkSelected ? "✓" : ""}</span>}
          <span className="session__title">{session.title}</span>
          <small>{session.sessionId === selectedSessionId && activeRun ? "运行中" : "会话"}</small>
        </button>;
        })}
        {selectionRect && <div
          className="session-selection-box"
          aria-hidden="true"
          style={{
            left: selectionRect.left,
            top: selectionRect.top,
            width: selectionRect.right - selectionRect.left,
            height: selectionRect.bottom - selectionRect.top,
          }}
        />}
      </nav>
    </Drawer>

    <header className="utility-toolbar" aria-label="工作台工具栏">
      <div className="utility-toolbar__actions">
        {view === "scheduler" && <button
          className="utility-toolbar__button utility-toolbar__sessions"
          type="button"
          aria-label={panelExpanded("sessions") ? "折叠会话栏" : "展开会话栏"}
          aria-controls="drawer-sessions"
          aria-expanded={panelExpanded("sessions")}
          onClick={(event) => togglePanel("sessions", event)}
        >会话</button>}
        <button
          className="utility-toolbar__button"
          type="button"
          ref={memoryTrigger}
          aria-controls="memory-drawer"
          aria-expanded={memoryDrawerOpen}
          title={memoryHealth === "unavailable" ? "记忆存储不可用；打开记忆可重试" : undefined}
          onClick={() => setMemoryDrawerOpen((current) => !current)}
        >记忆{memoryHealth === "unavailable" && <span className="utility-toolbar__memory-status" aria-label="记忆存储不可用" />}</button>
        <button
          className="utility-toolbar__button"
          type="button"
          aria-pressed={view === "scheduler"}
          onClick={() => setView((current) => current === "scheduler" ? "chat" : "scheduler")}
        >定时任务</button>
        <button
          className="utility-toolbar__button"
          type="button"
          aria-pressed={view === "chat" && showArchivedSessions}
          onClick={() => {
            const wasScheduler = view === "scheduler";
            setView("chat");
            if (!wasScheduler || !showArchivedSessions) void toggleArchivedSessions();
          }}
        >已归档会话</button>
        <div className="settings-launcher" ref={settingsMenu}>
          <button
            className="utility-toolbar__button"
            type="button"
            aria-label="外观与 API 设置"
            aria-expanded={settingsOpen}
            onClick={() => setSettingsOpen((current) => !current)}
          >设置</button>
          {settingsOpen && <section className="settings-popover" aria-label="外观与 API 设置">
            <p className="eyebrow">主题</p>
            <div className="theme-options" role="group" aria-label="选择主题">
              {([
                ["light", "浅色"],
                ["dark", "深色"],
                ["system", "跟随系统"],
              ] as const).map(([value, label]) => <button
                type="button"
                key={value}
                className={theme === value ? "is-active" : ""}
                aria-pressed={theme === value}
                onClick={() => setTheme(value)}
              >{label}</button>)}
            </div>
            <button className="settings-popover__api" type="button" onClick={openProviderDialog}>
              <span>API 设置</span>
              <small>{providerSettings?.model ?? "加载中…"}</small>
            </button>
          </section>}
        </div>
      </div>
    </header>

    <MemoryDrawer
      api={api as ApiClient}
      open={memoryDrawerOpen}
      refreshToken={memoryRefreshToken}
      memoryHealth={memoryHealth}
      onMemoryUnavailable={markMemoryUnavailable}
      onRetryHealth={refreshMemoryHealth}
      onClose={() => {
        setMemoryDrawerOpen(false);
        window.setTimeout(() => memoryTrigger.current?.focus(), 0);
      }}
    />

    {sessionMenu && <div
      className="session-context-menu"
      ref={sessionMenuElement}
      role="menu"
      aria-label={`${sessionMenu.title} 的会话操作`}
      style={{ left: sessionMenu.left, top: sessionMenu.top }}
    >
      {!showArchivedSessions && <>
        <button type="button" role="menuitem" onClick={() => openSessionDialog("rename")}>重命名</button>
        <button className="session-context-menu__danger" type="button" role="menuitem" onClick={() => openSessionDialog("archive")}>归档</button>
      </>}
      {showArchivedSessions && <>
        <button type="button" role="menuitem" onClick={() => void restoreSession(sessionMenu)}>恢复</button>
        <button className="session-context-menu__danger" type="button" role="menuitem" onClick={() => openSessionDialog("purge")}>永久删除</button>
      </>}
    </div>}

    {sessionDialog && <div
      className="session-dialog-backdrop"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget && !sessionMutationBusy) setSessionDialog(null);
      }}
    >
      <section className="session-dialog" role="dialog" aria-modal="true" aria-labelledby="session-dialog-title">
        {sessionDialog.kind === "rename" ? <form onSubmit={(event) => {
          event.preventDefault();
          void renameSession();
        }}>
          <p className="eyebrow">Session</p>
          <h2 id="session-dialog-title">重命名会话</h2>
          <label htmlFor="session-title">会话名称</label>
          <input
            id="session-title"
            autoFocus
            maxLength={200}
            value={renameTitle}
            onChange={(event) => setRenameTitle(event.target.value)}
            disabled={sessionMutationBusy}
          />
          <div className="session-dialog__actions">
            <button type="button" onClick={() => setSessionDialog(null)} disabled={sessionMutationBusy}>取消</button>
            <button className="session-dialog__primary" type="submit" disabled={!renameTitle.trim() || sessionMutationBusy}>保存</button>
          </div>
        </form> : sessionDialog.kind === "archive" ? <div>
          <p className="eyebrow">Session</p>
          <h2 id="session-dialog-title">归档会话？</h2>
          <p className="session-dialog__copy">归档会停止此会话正在排队和运行的任务，并移入已归档会话。</p>
          <div className="session-dialog__actions">
            <button type="button" onClick={() => setSessionDialog(null)} disabled={sessionMutationBusy}>取消</button>
            <button className="session-dialog__delete" type="button" onClick={() => void archiveSession()} disabled={sessionMutationBusy}>归档会话</button>
          </div>
        </div> : <div>
          <p className="eyebrow">Session</p>
          <h2 id="session-dialog-title">永久删除会话？</h2>
          <p className="session-dialog__copy">“{sessionDialog.session.title}”的会话、定时任务执行记录、附件和下载快照将从本机永久删除。此操作无法撤销。</p>
          <div className="session-dialog__actions">
            <button type="button" onClick={() => setSessionDialog(null)} disabled={sessionMutationBusy}>取消</button>
            <button className="session-dialog__delete" type="button" onClick={() => void deleteSession()} disabled={sessionMutationBusy}>永久删除</button>
          </div>
        </div>}
      </section>
    </div>}

    {bulkDeleteConfirm && <div
      className="session-dialog-backdrop"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget && !bulkDeleting) setBulkDeleteConfirm(false);
      }}
    >
      <section className="session-dialog" role="dialog" aria-modal="true" aria-labelledby="bulk-delete-title">
        <p className="eyebrow">Session</p>
        <h2 id="bulk-delete-title">归档 {selectedSessionIds.length} 个会话？</h2>
        <p className="session-dialog__copy">所选会话将移入已归档会话，之后可从归档区恢复；失败项会保留在当前列表。</p>
        <div className="session-dialog__actions">
          <button type="button" onClick={() => setBulkDeleteConfirm(false)} disabled={bulkDeleting}>取消</button>
          <button className="session-dialog__delete" type="button" onClick={() => void deleteSelectedSessions()} disabled={bulkDeleting}>
            {bulkDeleting ? "正在归档…" : `归档 ${selectedSessionIds.length} 项`}
          </button>
        </div>
      </section>
    </div>}

    {view === "chat" ? <>
    <section className="chat" aria-label="聊天">
      <header className="chat__header">
        <button
          className="panel-control panel-control--sessions"
          type="button"
          aria-label={panelExpanded("sessions") ? "折叠会话栏" : "展开会话栏"}
          aria-controls="drawer-sessions"
          aria-expanded={panelExpanded("sessions")}
          onClick={(event) => togglePanel("sessions", event)}
        >
          {panelExpanded("sessions") ? "‹" : "☰"}
        </button>
        <div className="chat__title"><p className="eyebrow">当前会话</p><h1>{detail?.title ?? "正在加载"}</h1></div>
        <div className="chat__header-actions">
          {activeRun && <span className="run-badge">运行中</span>}
          <button
            className="soul-config-button"
            type="button"
            onClick={() => void openSoulDialog()}
            disabled={soulBusy}
          >Soul</button>
          <button
            className="panel-control panel-control--details"
            type="button"
            aria-label={panelExpanded("details") ? "折叠活动栏" : "展开活动栏"}
            aria-controls="drawer-details"
            aria-expanded={panelExpanded("details")}
            onClick={(event) => togglePanel("details", event)}
          >
            {panelExpanded("details") ? "›" : "☷"}{attachments.length > 0 && <sup>{attachments.length}</sup>}
          </button>
        </div>
      </header>
      <div className="status-track" role="status"><span className="pulse" />{connectionStatus ?? statusText}<span>·</span> 已用时 {formatElapsed(elapsedMs)}</div>
      {error && <div className="error" role="alert">{error}</div>}
      <div className="history" ref={history} onScroll={onHistoryScroll} aria-live="polite">
        {detail?.messages.map((message) => {
          if (message.role === "user") return <article className="message message--user" key={message.messageId}><Markdown>{message.content}</Markdown></article>;
          if (message.role === "system") return <SystemEvent label={message.content} key={message.messageId} />;
          if (message.role === "tool") {
            if (message.toolCallId && (toolCallsById.has(message.toolCallId) || liveToolCallIds.has(message.toolCallId))) return null;
            return <ToolAccordion
              key={message.messageId}
              name={message.toolName ?? "工具"}
              input=""
              output={message.content}
              errorCode={message.toolErrorCode}
              status={message.toolResultStatus ?? (message.toolErrorCode ? "error" : "success")}
              durationMs={null}
              resources={normalizeToolResources(message.resources)}
              onWorkspace={focusWorkspaceArea}
            />;
          }
          const hasLiveTool = message.toolCalls.some((call) => liveToolCallIds.has(call.callId));
          const completedSkillUsages = skillUsagesByOutputMessageId.get(message.messageId) ?? [];
          const roundDuration = formatRoundDuration(
            roundDurationByAssistantMessageId.get(message.messageId) ?? null,
          );
          return <div className="timeline-group" key={message.messageId}>
            {!hasLiveTool && <>
              {roundDuration && <div className="round-duration">已处理 {roundDuration}</div>}
              {completedSkillUsages.map((usage) => <SkillAccordion
                key={`${usage.requestId}-${usage.reference.skillId}-${usage.selectionOrder}`}
                skillId={usage.reference.skillId}
                source={usage.source}
                reason={usage.reason}
                output={message.content}
                status="success"
                resourcePaths={usage.resourcePaths}
              />)}
              <AssistantBubble content={message.content} reasoning={message.reasoningContent} />
            </>}
            {message.toolCalls.filter((call) => !liveToolCallIds.has(call.callId)).map((call) => {
              const result = toolResultsByCallId.get(call.callId);
              return <ToolAccordion
                key={call.callId}
                name={call.name}
                input={call.arguments}
                output={result?.content ?? ""}
                errorCode={result?.toolErrorCode ?? null}
                status={result?.toolResultStatus ?? "running"}
                durationMs={null}
                resources={normalizeToolResources(result?.resources)}
                onWorkspace={focusWorkspaceArea}
              />;
            })}
          </div>;
        })}
        {selectedSessionId && (commandTimeline[selectedSessionId] ?? []).map((item) => <div
          className="timeline-group"
          key={item.id}
        >
          <article className="message message--user"><Markdown>{item.command}</Markdown></article>
          <article
            className={`command-card ${item.receipt?.isError ? "command-card--error" : ""}`}
            role="status"
          >
            <header>
              <strong>{item.receipt?.command ?? item.command}</strong>
              <span>{item.receipt ? (item.receipt.isError ? "错误" : "完成") : "运行中"}</span>
            </header>
            <pre>{item.receipt ? item.receipt.lines.join("\n") : "等待命令结果"}</pre>
          </article>
        </div>)}
        {liveItems.map((item) => item.kind === "assistant"
          ? <AssistantBubble key={item.id} content={item.content} streaming={item.streaming} />
          : item.kind === "system"
            ? <SystemEvent key={item.id} label={item.label} />
            : item.kind === "skill"
              ? <SkillAccordion
                  key={item.id}
                  skillId={item.skillId}
                  source={item.source}
                  reason={item.reason}
                  output={item.output}
                  status={item.status}
                  resourcePaths={item.resourcePaths}
                />
            : <ToolAccordion
                key={item.id}
                name={item.name}
                input={item.input}
                output={item.output}
                errorCode={item.errorCode}
                status={item.status}
                durationMs={item.durationMs}
                resources={item.resources}
                onWorkspace={focusWorkspaceArea}
              />)}
        {!detail && <p className="empty">正在读取本地 Session。</p>}
      </div>
      {queuedRequests.length > 0 && <section className="request-queue" aria-label={`请求队列，共 ${queuedRequests.length} 项`}>
        <header className="request-queue__header"><strong>请求队列</strong><span>{queuedRequests.length}</span></header>
        <div className="request-queue__items">
          {queuedRequests.map((request, index) => {
            const requestAttachments = request.attachmentRefs
              .map((attachmentId) => attachments.find((item) => item.attachmentId === attachmentId)?.displayName)
              .filter((value): value is string => Boolean(value));
            const canSteer = Boolean(activeRun) && request.attachmentRefs.length === 0 && request.skillRefs.length === 0;
            const steerHint = !activeRun
              ? "当前没有运行中的请求"
              : canSteer
                ? "从队列移除并作为当前运行的引导"
                : "带附件或 Skill 的请求不能改为引导";
            return <article className="queued-request" key={request.requestId}>
              <span className="queued-request__icon" aria-hidden="true">↳</span>
              <div className="queued-request__content">
                <strong title={request.text}>{request.text}</strong>
                <small>
                  第 {index + 1} 项 · {formatTime(request.submittedAt)}
                  {request.source === "scheduler" ? " · 定时任务" : ""}
                  {requestAttachments.length ? ` · 附件：${requestAttachments.join("、")}` : ""}
                  {request.skillRefs.length ? ` · Skill：${request.skillRefs.map((item) => item.skillId).join("、")}` : ""}
                </small>
              </div>
              <div className="queued-request__actions">
                <button
                  type="button"
                  onClick={() => void steerQueuedRequest(request)}
                  disabled={busy || isArchivedSession || !canSteer}
                  title={steerHint}
                >↪ 引导</button>
                <button
                  className="queued-request__delete"
                  type="button"
                  onClick={() => void cancelQueuedRequest(request.requestId)}
                  disabled={busy || isArchivedSession}
                  aria-label={`撤回排队请求：${request.text}`}
                  title="撤回排队请求"
                >删除</button>
              </div>
            </article>;
          })}
        </div>
      </section>}
      <form className="composer" onSubmit={(event) => void submit(event)}>
        <label htmlFor="message">输入消息</label>
        <textarea
          ref={textArea}
          id="message"
          value={text}
          aria-controls={commandCompletions.length ? "command-completions" : undefined}
          aria-activedescendant={commandCompletions.length ? `command-suggestion-${commandIndex}` : undefined}
          onChange={(event) => {
            setText(event.target.value);
            setCommandIndex(0);
            setCommandCompletionDismissed(false);
          }}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing) return;
            const suggestion = commandCompletions[commandIndex];
            if (event.key === "Escape" && commandCompletions.length) {
              event.preventDefault();
              setCommandCompletionDismissed(true);
              return;
            }
            if (event.key === "ArrowDown" && commandCompletions.length) {
              event.preventDefault();
              setCommandIndex((current) => moveSuggestion(current, 1, commandCompletions.length));
              return;
            }
            if (event.key === "ArrowUp" && commandCompletions.length) {
              event.preventDefault();
              setCommandIndex((current) => moveSuggestion(current, -1, commandCompletions.length));
              return;
            }
            if ((event.key === "Tab" || event.key === "Enter") && suggestion && text !== suggestion.value) {
              event.preventDefault();
              setText(applySuggestion(text, suggestion.value));
              setCommandIndex(0);
              setCommandCompletionDismissed(false);
              return;
            }
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder="给 SJTUClaw 发消息…"
          rows={1}
          disabled={!selectedSessionId || busy || isArchivedSession}
        />
        {commandCompletions.length > 0 && <div
          className="command-completions"
          id="command-completions"
          role="listbox"
          aria-label="命令补全"
        >
          {commandCompletions.map((suggestion, index) => <button
            className={index === commandIndex ? "command-completion command-completion--active" : "command-completion"}
            id={`command-suggestion-${index}`}
            key={suggestion.value}
            type="button"
            role="option"
            aria-selected={index === commandIndex}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => {
              setText(applySuggestion(text, suggestion.value));
              setCommandIndex(0);
              setCommandCompletionDismissed(false);
              window.requestAnimationFrame(() => textArea.current?.focus());
            }}
          >
            <strong>{suggestion.value}</strong><small>{suggestion.description}</small>
          </button>)}
        </div>}
        <input
          ref={fileInput}
          className="visually-hidden"
          type="file"
          multiple
          accept="text/*,.txt,.md,.markdown,.csv,.tsv,.json,.jsonl,.log,.xml,.yaml,.yml,.toml,.ini,.cfg,.conf,.rst,application/pdf,.pdf"
          onChange={(event) => void uploadFiles(event)}
          disabled={isArchivedSession}
        />
        {selectedAttachmentIds.length > 0 && <div className="attachment-chips" aria-label="本轮已选附件">{selectedAttachmentIds.map((attachmentId) => {
          const attachment = attachments.find((item) => item.attachmentId === attachmentId);
          return attachment && <button className="attachment-chip" type="button" key={attachmentId} onClick={() => toggleAttachment(attachmentId)}>{attachment.displayName} ×</button>;
        })}</div>}
        <div className="composer__footer">
          <div className="add-menu-wrap" ref={addMenu}>
            <button
              className="add-button"
              type="button"
              aria-label="添加"
              aria-expanded={addMenuOpen}
              onClick={() => {
                setAddMenuView("root");
                setAddMenuOpen((current) => !current);
              }}
              disabled={!selectedSessionId || busy || isArchivedSession}
            >＋</button>
            {addMenuOpen && <div className="add-menu" role="menu" aria-label={addMenuView === "root" ? "添加" : "选择 Skill"}>
              {addMenuView === "root" ? <>
                <button type="button" role="menuitem" onClick={() => {
                  setAddMenuOpen(false);
                  fileInput.current?.click();
                }}>添加文本 / PDF</button>
                <button type="button" role="menuitem" onClick={() => setAddMenuView("skills")}>添加 Skill</button>
              </> : <>
                <button className="add-menu__back" type="button" role="menuitem" onClick={() => setAddMenuView("root")}>← 返回</button>
                <div className="add-menu__title">本地 Skill</div>
                {manualSkillOptions(skills).map((skill) => <button
                  type="button"
                  role="menuitem"
                  key={skill.skillId}
                  disabled={!skill.available}
                  title={skill.unavailableReason ?? undefined}
                  onClick={() => insertSkillMention(skill.skillId)}
                >
                  <strong>{skill.skillId}</strong><small>{skill.available ? `v${skill.version} · ${skill.description}` : skill.unavailableReason}</small>
                </button>)}
              </>}
            </div>}
          </div>
          <div className="composer__actions">
            {activeRun && <><button className="steer" type="button" onClick={() => void steer()} disabled={!text.trim() || busy || isArchivedSession}>引导</button><button className="stop" type="button" onClick={() => void stop()} disabled={isArchivedSession}>停止</button></>}
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
                <span aria-hidden="true">▾</span>
              </button>
              {providerQuickOpen && <div className="provider-quick__menu" role="menu" aria-label="DeepSeek 模型与回答模式">
                <p>模型</p>
                {deepSeekModels.map(({ id, label }) => <button
                  type="button"
                  role="menuitemradio"
                  key={id}
                  aria-checked={providerSettings.model === id}
                  disabled={providerBusy}
                  onClick={() => void saveDeepSeekQuickSettings({ model: id })}
                ><span>{label}</span><span aria-hidden="true">{providerSettings.model === id ? "✓" : ""}</span></button>)}
                <p>回答模式</p>
                {deepSeekThinkingModes.map(({ enabled, label }) => <button
                  type="button"
                  role="menuitemradio"
                  key={String(enabled)}
                  aria-checked={providerSettings.thinkingEnabled === enabled}
                  disabled={providerBusy}
                  onClick={() => void saveDeepSeekQuickSettings({ thinkingEnabled: enabled })}
                ><span>{label}</span><span aria-hidden="true">{providerSettings.thinkingEnabled === enabled ? "✓" : ""}</span></button>)}
                {providerQuickError && <small className="provider-quick__error" role="alert">{providerQuickError}</small>}
              </div>}
            </div>}
            <button className="send" type="submit" disabled={!text.trim() || busy || isArchivedSession}>发送 ↵</button>
          </div>
        </div>
      </form>
    </section>

    <Drawer name="details" open={drawer === "details"} onClose={close}>
      <section className="detail-section workspace-section" ref={workspaceSection}>
        <p className="eyebrow">工作区</p>
        {detail?.workspace && !workspaceEditing ? <div className="workspace-bound">
          <span title={detail.workspace.displayPath}>{detail.workspace.displayPath}</span>
          <div>
            <button ref={workspaceChange} type="button" onClick={() => {
              setWorkspaceEditing(true);
              window.requestAnimationFrame(() => workspacePicker.current?.focus());
            }} disabled={workspaceBusy || isArchivedSession}>更换</button>
            <button type="button" onClick={() => void unbindWorkspace()} disabled={workspaceBusy || isArchivedSession}>解绑</button>
          </div>
        </div> : <>
          {!detail?.workspace && <p className="workspace-hint">未绑定；写入、Shell 和下载工具不可用。</p>}
          <div className="workspace-picker">
            <button
              ref={workspacePicker}
              type="button"
              onClick={() => void pickWorkspace()}
              disabled={!selectedSessionId || workspaceBusy || isArchivedSession}
            >{workspaceBusy ? "正在选择…" : "选择目录"}</button>
            {detail?.workspace && <button className="workspace-cancel" type="button" onClick={() => {
              setWorkspaceEditing(false);
            }}>取消更换</button>}
          </div>
        </>}
      </section>
      {detail?.pendingApproval && <section className="detail-section">
        <p className="eyebrow">需要本次审批</p>
        <div className="activity"><span className="pulse" aria-hidden="true" />{detail.pendingApproval.toolName} · {detail.pendingApproval.safetyLevel}</div>
        <p className="muted">{detail.pendingApproval.reason}</p>
        {detail.pendingApproval.targets.length > 0 && <p className="muted">目标：{detail.pendingApproval.targets.join("、")}</p>}
        <p className="muted">预览：{detail.pendingApproval.preview}</p>
        <p className="muted">到期：{formatTime(detail.pendingApproval.expiresAt)}</p>
        <label htmlFor="approval-reason" className="eyebrow">拒绝原因（可选）</label>
        <textarea
          id="approval-reason"
          value={approvalReason}
          onChange={(event) => setApprovalReason(event.target.value)}
          maxLength={1000}
          rows={2}
          placeholder="可说明拒绝原因"
          disabled={approvalBusy || isArchivedSession || decidedApprovalId === detail.pendingApproval.approvalId}
        />
        <button className="send" type="button" onClick={() => void decideApproval("approve")} disabled={approvalBusy || isArchivedSession || decidedApprovalId === detail.pendingApproval.approvalId}>仅批准本次</button>
        <button className="stop" type="button" onClick={() => void decideApproval("deny")} disabled={approvalBusy || isArchivedSession || decidedApprovalId === detail.pendingApproval.approvalId}>拒绝</button>
        {decidedApprovalId === detail.pendingApproval.approvalId && <p className="muted">决定已提交，等待服务端恢复该 Run。</p>}
      </section>}
      <section className="detail-section attachments">
        <div className="section-title">
          <p className="eyebrow">本会话附件</p>
          <div className="attachment-selection">
            <span aria-label={`共 ${attachments.length} 个附件，已选 ${selectedAttachmentIds.length} 个`}>
              {selectedAttachmentIds.length > 0 ? `已选 ${selectedAttachmentIds.length}` : `${attachments.length} 个附件`}
            </span>
            {selectedAttachmentIds.length > 0 && <button
              className="attachment-selection__clear"
              type="button"
              onClick={() => setSelectedAttachmentIds([])}
              disabled={isArchivedSession}
            >
              取消本轮选择
            </button>}
          </div>
        </div>
        {attachments.length === 0 && <p className="muted">尚无附件</p>}
        {attachments.map((attachment) => <button
          className={`file ${selectedAttachmentIds.includes(attachment.attachmentId) ? "file--selected" : ""}`}
          type="button"
          key={attachment.attachmentId}
          onClick={() => toggleAttachment(attachment.attachmentId)}
          aria-pressed={selectedAttachmentIds.includes(attachment.attachmentId)}
          disabled={isArchivedSession || attachment.status !== "ready"}
        >
          <span>{attachment.kind.toUpperCase()}</span><div><strong>{attachment.displayName}</strong><small>{attachment.status === "ready" ? formatBytes(attachment.sizeBytes) : attachment.status}</small></div>
        </button>)}
      </section>
      <TokenDashboards detail={detail} />
    </Drawer>
    </> : <SchedulerView
      api={api as ApiClient}
      sessions={sessions}
      archivedSessions={archivedSessions}
      selectedSessionId={selectedSessionId}
    />}

    {providerDialogOpen && <div
      className="session-dialog-backdrop"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget && !providerBusy) setProviderDialogOpen(false);
      }}
    >
      <section className="session-dialog provider-dialog" role="dialog" aria-modal="true" aria-labelledby="provider-settings-title">
        <form onSubmit={(event) => {
          event.preventDefault();
          void saveProviderSettings();
        }}>
          <p className="eyebrow">运行时 API</p>
          <h2 id="provider-settings-title">切换模型服务</h2>
          <p className="session-dialog__copy">保存后立即用于下一条新消息；正在进行的回复不会中断。</p>
          {providerError && <p className="provider-dialog__error" role="alert">{providerError}</p>}
          <label htmlFor="provider-kind">协议</label>
          <select
            id="provider-kind"
            value={providerDraft?.provider ?? "deepseek"}
            disabled={providerBusy}
            onChange={(event) => {
              const provider = event.target.value as ProviderKind;
              const defaults = providerDefaults[provider];
              setProviderDraft((current) => current && {
                ...current,
                provider,
                model: defaults.model,
                baseUrl: defaults.baseUrl,
                tokenCounterMode: provider === "deepseek" ? "deepseek" : "approximate",
              });
            }}
          >
            <option value="deepseek">DeepSeek</option>
            <option value="openai_compatible">OpenAI 兼容</option>
            <option value="anthropic">Anthropic</option>
          </select>
          <label htmlFor="provider-model">模型</label>
          <input
            id="provider-model"
            value={providerDraft?.model ?? ""}
            maxLength={200}
            disabled={providerBusy}
            onChange={(event) => setProviderDraft((current) => current && { ...current, model: event.target.value })}
          />
          <label htmlFor="provider-base-url">Base URL</label>
          <input
            id="provider-base-url"
            type="url"
            value={providerDraft?.baseUrl ?? ""}
            maxLength={2000}
            disabled={providerBusy}
            onChange={(event) => setProviderDraft((current) => current && { ...current, baseUrl: event.target.value })}
          />
          <label htmlFor="provider-api-key">API Key（可留空）</label>
          <input
            id="provider-api-key"
            type="password"
            value={providerApiKey}
            autoComplete="off"
            placeholder={providerSettings?.keySaved ? "已保存；留空则不修改" : "请输入 API Key"}
            maxLength={2000}
            disabled={providerBusy}
            onChange={(event) => setProviderApiKey(event.target.value)}
          />
          <p className="provider-dialog__hint">
            {providerDraft?.provider === "deepseek" ? "DeepSeek 使用精确 tokenizer。" : "其他服务使用近似 token 估算。"}
          </p>
          <div className="session-dialog__actions">
            <button type="button" onClick={() => setProviderDialogOpen(false)} disabled={providerBusy}>取消</button>
            <button
              className="session-dialog__primary"
              type="submit"
              disabled={providerBusy || !providerDraft?.model.trim() || !providerDraft.baseUrl.trim()}
            >{providerBusy ? "正在保存…" : "保存并切换"}</button>
          </div>
        </form>
      </section>
    </div>}

    {soulDialogOpen && <div
      className="session-dialog-backdrop"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget && !soulBusy) setSoulDialogOpen(false);
      }}
    >
      <section className="session-dialog provider-dialog soul-dialog" role="dialog" aria-modal="true" aria-labelledby="soul-settings-title">
        <form onSubmit={(event) => {
          event.preventDefault();
          void saveSoul();
        }}>
          <p className="eyebrow">Prompt 配置</p>
          <h2 id="soul-settings-title">编辑 Soul</h2>
          <p className="session-dialog__copy">保存后，下一条新消息会立即使用新版 Soul；已经开始的回复不会中断。</p>
          {soulError && <p className="provider-dialog__error" role="alert">{soulError}</p>}
          <label htmlFor="soul-content">Soul Markdown</label>
          <textarea
            id="soul-content"
            value={soulDraft}
            rows={14}
            maxLength={50_000}
            disabled={soulBusy}
            onChange={(event) => setSoulDraft(event.target.value)}
          />
          <section className="system-prompt-notice" aria-label="System Prompt 配置说明">
            <strong>System Prompt</strong>
            <p>为避免误改，网页不提供编辑。请自行修改配置文件：</p>
            <code>{promptSettings?.systemPrompt.path ?? "正在读取配置路径…"}</code>
            <p>{promptSettings?.systemPrompt.restartRequired ? "修改后请重启 Gateway，新的 System Prompt 才会生效。" : "请重启 Gateway 后生效。"}</p>
          </section>
          <div className="session-dialog__actions">
            <button type="button" onClick={() => setSoulDialogOpen(false)} disabled={soulBusy}>取消</button>
            <button className="session-dialog__primary" type="submit" disabled={soulBusy}>{soulBusy ? "正在保存…" : "保存 Soul"}</button>
          </div>
        </form>
      </section>
    </div>}
  </main>;
}

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, { credentials: "same-origin", ...init });
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { code?: string; message?: string };
    throw new ApiError(body.code ?? "REQUEST_FAILED", body.message ?? "本地请求失败");
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

async function exchangeBootstrapFragment(): Promise<void> {
  const token = new URLSearchParams(window.location.hash.slice(1)).get("bootstrap");
  if (!token) return;
  await api<void>("/auth/bootstrap", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
}

function safeUrl(url: string): string {
  if (url.startsWith("/") || url.startsWith("#")) return url;
  try {
    const parsed = new URL(url);
    return ["http:", "https:", "mailto:"].includes(parsed.protocol) ? url : "#";
  } catch {
    return "#";
  }
}

function uploadContentType(file: File): string {
  if (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")) return "application/pdf";
  return "text/plain; charset=utf-8";
}

function formatElapsed(milliseconds: number): string {
  const seconds = Math.floor(milliseconds / 1_000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatTime(value: string): string {
  const time = new Date(value);
  if (Number.isNaN(time.valueOf())) return "时间未知";
  return time.toLocaleString("zh-CN", { hour12: false });
}

function formatBytes(value: number | null): string {
  if (value === null) return "处理中";
  return value < 1024 ? `${value} B` : `${Math.ceil(value / 1024)} KB`;
}

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${trimDecimal(value / 1_000_000, 2)}M`;
  if (value >= 10_000) return `${trimDecimal(value / 1_000, 0)}K`;
  if (value >= 1_000) return `${trimDecimal(value / 1_000, 1)}K`;
  return String(value);
}

function trimDecimal(value: number, digits: number): string {
  return value.toFixed(digits).replace(/\.?0+$/, "");
}

function contextRingBackground(context: ContextWindow): string {
  const limit = Math.max(context.limitTokens, 1);
  const currentEnd = Math.min(context.currentTokens / limit * 100, 100);
  const reserveEnd = Math.min(
    (context.currentTokens + context.outputReserveTokens) / limit * 100,
    100,
  );
  return `conic-gradient(var(--blue) 0 ${currentEnd}%, #b9c8e8 ${currentEnd}% ${reserveEnd}%, #edf1f7 ${reserveEnd}% 100%)`;
}

function finishStreamingItem(items: LiveTimelineItem[]): LiveTimelineItem[] {
  const last = items.at(-1);
  if (last?.kind !== "assistant" || !last.streaming) return items;
  return [...items.slice(0, -1), { ...last, streaming: false }];
}

function findLastItemIndex<T>(items: T[], predicate: (item: T) => boolean): number {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (predicate(items[index])) return index;
  }
  return -1;
}

function parseEventTime(value: string): number | null {
  const milliseconds = Date.parse(value);
  return Number.isNaN(milliseconds) ? null : milliseconds;
}

function formatToolDuration(milliseconds: number): string {
  return milliseconds < 1_000 ? `${milliseconds} ms` : `${trimDecimal(milliseconds / 1_000, 1)} s`;
}

function skillSourceText(source: SkillUsage["source"]): string {
  if (source === "auto") return "自动选择";
  if (source === "scheduled_explicit") return "定时任务指定";
  return "手动指定";
}

function formatToolContent(value: string, emptyText: string): string {
  if (!value) return emptyText;
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : "本地请求失败";
}
