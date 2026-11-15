import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

export type ApiClient = <T>(path: string, init?: RequestInit) => Promise<T>;

export type SchedulerSession = {
  sessionId: string;
  title: string;
};

type ScheduleKind = "once" | "interval" | "daily" | "weekly";

type TaskSchedule = {
  kind: ScheduleKind;
  anchorAt: string;
  intervalSeconds: number | null;
  timezone: string | null;
  hour: number | null;
  minute: number | null;
  weekdays: number[];
};

type SchedulerTask = {
  taskId: string;
  sessionId: string;
  instruction: string;
  schedule: TaskSchedule;
  status: string;
  nextRunAt: string | null;
  workspaceBindingRevision: number | null;
  managedPermissions: string[];
  skillRefs: Array<{ skillId: string; version: string }>;
  archivedAt: string | null;
  archivedReason: string | null;
};

type WorkspaceBinding = {
  displayPath: string;
  revision: number;
};

type Occurrence = {
  occurrenceId: string;
  sequenceNumber: number;
  status: string;
  scheduledFor: string;
  updatedAt: string;
  errorCode: string | null;
  missedCount: number;
};

type OccurrencePage = {
  occurrences: Occurrence[];
  cursor?: string | null;
  nextCursor?: string | null;
};

type ScheduleDraft = {
  kind: ScheduleKind;
  anchorAt: string;
  intervalSeconds: string;
  timezone: string;
  hour: string;
  minute: string;
  weekdays: number[];
};

const managedPermissionOptions = [
  ["workspace_write", "修改 Workspace 文件"],
  ["shell", "运行 Shell 命令"],
  ["download", "下载文件"],
] as const;
const taskStatusLabels: Record<string, string> = {
  active: "等待执行",
  cancel_requested: "正在取消",
  cancelled: "已取消",
  completed: "已完成",
  failed: "执行失败",
};
const occurrenceStatusLabels: Record<string, string> = {
  planned: "等待执行",
  queued: "已进入队列",
  running: "执行中",
  completed: "已完成",
  failed: "执行失败",
  cancelled_before_start: "执行前已取消",
  skipped_overlap: "已跳过重复执行",
};
const weekdays = ["一", "二", "三", "四", "五", "六", "日"];

function defaultDraft(): ScheduleDraft {
  const soon = new Date(Date.now() + 60 * 60 * 1_000);
  return {
    kind: "once",
    anchorAt: toDateTimeLocal(soon),
    intervalSeconds: "300",
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    hour: String(soon.getHours()),
    minute: String(soon.getMinutes()),
    weekdays: [soon.getDay() === 0 ? 6 : soon.getDay() - 1],
  };
}

export function SchedulerView({
  api,
  sessions,
  archivedSessions,
  selectedSessionId,
}: {
  api: ApiClient;
  sessions: SchedulerSession[];
  archivedSessions: SchedulerSession[];
  selectedSessionId: string | null;
}) {
  const [tasks, setTasks] = useState<SchedulerTask[]>([]);
  const [selectedTask, setSelectedTask] = useState<SchedulerTask | null>(null);
  const [occurrences, setOccurrences] = useState<Occurrence[]>([]);
  const [occurrenceCursor, setOccurrenceCursor] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [taskQuery, setTaskQuery] = useState("");
  const [taskStatus, setTaskStatus] = useState("all");
  const [showArchived, setShowArchived] = useState(false);
  const [editingTask, setEditingTask] = useState<SchedulerTask | null>(null);
  const [sessionId, setSessionId] = useState(selectedSessionId ?? "");
  const [instruction, setInstruction] = useState("");
  const [schedule, setSchedule] = useState<ScheduleDraft>(defaultDraft);
  const [sessionWorkspaces, setSessionWorkspaces] = useState<Record<string, WorkspaceBinding | null>>({});
  const [useSessionWorkspace, setUseSessionWorkspace] = useState(true);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [managedPermissions, setManagedPermissions] = useState<string[]>([]);
  const [skillRefs, setSkillRefs] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const taskRequestGeneration = useRef(0);

  const selectedTaskId = selectedTask?.taskId ?? null;
  const allSessions = useMemo(() => [...sessions, ...archivedSessions], [archivedSessions, sessions]);
  const visibleTasks = tasks.filter((task) => {
    const title = allSessions.find((session) => session.sessionId === task.sessionId)?.title ?? "";
    const matchesText = `${title} ${task.instruction}`
      .toLocaleLowerCase()
      .includes(taskQuery.trim().toLocaleLowerCase());
    return matchesText && (taskStatus === "all" || task.status === taskStatus);
  });
  const selectedSessionTitle = useMemo(
    () => sessions.find((session) => session.sessionId === sessionId)?.title ?? "",
    [sessionId, sessions],
  );
  const selectedSessionWorkspace = sessionId ? sessionWorkspaces[sessionId] ?? null : null;

  useEffect(() => {
    const sessionIds = [...new Set(
      [sessionId, selectedTask?.sessionId].filter((value): value is string => Boolean(value)),
    )];
    const missingSessionIds = sessionIds.filter((value) => !Object.hasOwn(sessionWorkspaces, value));
    if (!missingSessionIds.length) return;
    let cancelled = false;
    if (missingSessionIds.includes(sessionId)) setWorkspaceLoading(true);
    void Promise.all(missingSessionIds.map(async (value) => {
      const response = await api<{ workspace: WorkspaceBinding | null }>(`/sessions/${value}`);
      return [value, response.workspace] as const;
    }))
      .then((entries) => {
        if (!cancelled) {
          setSessionWorkspaces((current) => ({ ...current, ...Object.fromEntries(entries) }));
        }
      })
      .catch((caught) => {
        if (!cancelled) setError(messageOf(caught));
      })
      .finally(() => {
        if (!cancelled) setWorkspaceLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [api, sessionId, selectedTask?.sessionId, sessionWorkspaces]);

  const loadTasks = async (
    archivedMode = showArchived,
    generation = taskRequestGeneration.current,
  ) => {
    const response = await api<{ tasks: SchedulerTask[] }>(`/tasks?archived=${archivedMode}`);
    if (generation !== taskRequestGeneration.current) return [];
    setTasks(response.tasks);
    return response.tasks;
  };

  const loadTask = async (taskId: string, generation = taskRequestGeneration.current) => {
    const response = await api<{ task: SchedulerTask }>(`/tasks/${taskId}`);
    if (generation === taskRequestGeneration.current) setSelectedTask(response.task);
    return response.task;
  };

  const loadOccurrences = async (
    taskId: string,
    cursor: string | null = null,
    generation = taskRequestGeneration.current,
  ) => {
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}&limit=25` : "?limit=25";
    const response = await api<OccurrencePage>(`/tasks/${taskId}/occurrences${query}`);
    if (generation !== taskRequestGeneration.current) return;
    setOccurrenceCursor(response.nextCursor ?? response.cursor ?? null);
    setOccurrences((current) => cursor ? [...current, ...response.occurrences] : response.occurrences);
  };

  const selectTask = async (taskId: string, requestedGeneration?: number) => {
    const generation = requestedGeneration ?? ++taskRequestGeneration.current;
    setSelectedTask(null);
    setOccurrences([]);
    setOccurrenceCursor(null);
    setBusy(true);
    try {
      await Promise.all([
        loadTask(taskId, generation),
        loadOccurrences(taskId, null, generation),
      ]);
      if (generation === taskRequestGeneration.current) setError(null);
    } catch (caught) {
      if (generation === taskRequestGeneration.current) setError(messageOf(caught));
    } finally {
      if (generation === taskRequestGeneration.current) setBusy(false);
    }
  };

  useEffect(() => {
    const archivedMode = showArchived;
    const generation = ++taskRequestGeneration.current;
    setBusy(false);
    setSelectedTask(null);
    setOccurrences([]);
    setOccurrenceCursor(null);
    setTasks([]);
    if (archivedMode) {
      setShowForm(false);
      setEditingTask(null);
    }
    void (async () => {
      try {
        const listed = await loadTasks(archivedMode, generation);
        if (listed.length && generation === taskRequestGeneration.current) {
          await selectTask(listed[0].taskId, generation);
        }
      } catch (caught) {
        if (generation === taskRequestGeneration.current) setError(messageOf(caught));
      }
    })();
  }, [showArchived]);

  const createTask = async (event: FormEvent) => {
    event.preventDefault();
    if (!sessionId || !instruction.trim()) return;
    setBusy(true);
    try {
      const response = await api<{ task: SchedulerTask }>(editingTask ? `/tasks/${editingTask.taskId}` : "/tasks", {
        method: editingTask ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sessionId,
          instruction: instruction.trim(),
          schedule: scheduleBody(schedule),
          workspaceBindingRevision: useSessionWorkspace ? selectedSessionWorkspace?.revision ?? null : null,
          managedPermissions: useSessionWorkspace && selectedSessionWorkspace ? managedPermissions : [],
          skillRefs: skillRefs.split(/\r?\n|,/).map((value) => value.trim()).filter(Boolean),
        }),
      });
      setShowForm(false);
      setEditingTask(null);
      setInstruction("");
      setSchedule(defaultDraft());
      setUseSessionWorkspace(true);
      setManagedPermissions([]);
      setSkillRefs("");
      await loadTasks();
      await selectTask(response.task.taskId);
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setBusy(false);
    }
  };

  const archiveTask = async () => {
    if (!selectedTask) return;
    setBusy(true);
    try {
      await api(`/tasks/${selectedTask.taskId}/cancel`, { method: "POST" });
      setSelectedTask(null);
      setOccurrences([]);
      await loadTasks();
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setBusy(false);
    }
  };

  const restoreTask = async () => {
    if (!selectedTask) return;
    setBusy(true);
    try {
      await api(`/tasks/${selectedTask.taskId}/restore`, { method: "POST" });
      taskRequestGeneration.current += 1;
      setSelectedTask(null);
      setOccurrences([]);
      setOccurrenceCursor(null);
      setShowArchived(false);
      setError(null);
    } catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  };

  const purgeTask = async () => {
    if (!selectedTask || !window.confirm("永久删除此任务及执行历史？此操作不可恢复。")) return;
    setBusy(true);
    try {
      await api(`/tasks/${selectedTask.taskId}`, { method: "DELETE" });
      setSelectedTask(null); setOccurrences([]);
      await loadTasks();
    } catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  };

  const beginEdit = (task: SchedulerTask) => {
    setEditingTask(task); setSessionId(task.sessionId); setInstruction(task.instruction);
    const anchor = new Date(task.schedule.anchorAt);
    setSchedule({
      kind: task.schedule.kind,
      anchorAt: toDateTimeLocal(Number.isNaN(anchor.getTime()) ? new Date() : anchor),
      intervalSeconds: String(task.schedule.intervalSeconds ?? 300),
      timezone: task.schedule.timezone ?? (Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"),
      hour: String(task.schedule.hour ?? anchor.getHours()),
      minute: String(task.schedule.minute ?? anchor.getMinutes()),
      weekdays: task.schedule.weekdays,
    });
    setUseSessionWorkspace(task.workspaceBindingRevision !== null);
    setManagedPermissions(task.managedPermissions);
    setSkillRefs(task.skillRefs.map((item) => item.skillId).join("\n"));
    setShowForm(true);
  };

  const pickTaskWorkspace = async () => {
    if (!sessionId || workspaceBusy) return;
    setWorkspaceBusy(true);
    try {
      const picked = await api<{ path: string | null }>(`/sessions/${sessionId}/workspace/pick`, {
        method: "POST",
      });
      if (!picked.path) return;
      const response = await api<{ workspace: WorkspaceBinding }>(`/sessions/${sessionId}/workspace`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root: picked.path }),
      });
      setSessionWorkspaces((current) => ({ ...current, [sessionId]: response.workspace }));
      setUseSessionWorkspace(true);
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setWorkspaceBusy(false);
    }
  };

  const retryOccurrence = async (occurrenceId: string) => {
    if (!selectedTask) return;
    setBusy(true);
    try {
      await api(`/tasks/${selectedTask.taskId}/occurrences/${occurrenceId}/retry`, { method: "POST" });
      await Promise.all([loadTask(selectedTask.taskId), loadOccurrences(selectedTask.taskId)]);
      setError(null);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setBusy(false);
    }
  };

  return <section className="scheduler-page" aria-label="定时任务">
    <header className="scheduler-page__header">
      <div><p className="eyebrow">自动执行</p><h1>定时任务</h1><p className="muted">任务由服务端执行，关闭此页面不会影响计划。</p></div>
      <div className="scheduler-page__actions"><button className="attach" type="button" onClick={() => {
        taskRequestGeneration.current += 1;
        setBusy(false);
        setSelectedTask(null);
        setOccurrences([]);
        setOccurrenceCursor(null);
        setTasks([]);
        setShowArchived((value) => !value);
      }}>{showArchived ? "普通任务" : "已归档任务"}</button>{!showArchived && <button className="send" type="button" onClick={() => { setEditingTask(null); setShowForm((value) => !value); }} aria-expanded={showForm}>新建任务</button>}</div>
    </header>
    {error && <div className="error scheduler-page__error" role="alert">{error}</div>}
    {showForm && <form className="task-form" onSubmit={(event) => void createTask(event)}>
      <div className="task-form__title"><h2>{editingTask ? "编辑定时任务" : "新建定时任务"}</h2><p>任务保存在本机，并由服务端按计划执行。</p></div>
      <label>所在会话<select value={sessionId} onChange={(event) => { setSessionId(event.target.value); setUseSessionWorkspace(true); }} required><option value="" disabled>选择会话</option>{sessions.map((session) => <option value={session.sessionId} key={session.sessionId}>{session.title}</option>)}</select></label>
      <label>重复方式<select value={schedule.kind} onChange={(event) => setSchedule((current) => ({ ...current, kind: event.target.value as ScheduleKind }))}><option value="once">仅执行一次</option><option value="interval">按固定间隔</option><option value="daily">每天</option><option value="weekly">每周</option></select></label>
      <label className="task-form__wide">任务内容<textarea value={instruction} onChange={(event) => setInstruction(event.target.value)} rows={3} required placeholder="任务执行时发送给 Agent 的内容" /></label>
      <label>首次执行时间（本地时间）<input type="datetime-local" value={schedule.anchorAt} onChange={(event) => setSchedule((current) => ({ ...current, anchorAt: event.target.value }))} required /></label>
      {schedule.kind === "interval" && <label>重复间隔（秒，至少 300）<input type="number" min="300" step="1" value={schedule.intervalSeconds} onChange={(event) => setSchedule((current) => ({ ...current, intervalSeconds: event.target.value }))} required /></label>}
      {(schedule.kind === "daily" || schedule.kind === "weekly") && <>
        <label>时区<input value={schedule.timezone} onChange={(event) => setSchedule((current) => ({ ...current, timezone: event.target.value }))} required /></label>
        <label>执行时间<input type="time" value={`${schedule.hour.padStart(2, "0")}:${schedule.minute.padStart(2, "0")}`} onChange={(event) => { const [hour, minute] = event.target.value.split(":"); setSchedule((current) => ({ ...current, hour, minute })); }} required /></label>
        {schedule.kind === "weekly" && <fieldset className="task-form__wide"><legend>执行日</legend><div className="weekday-picker">{weekdays.map((label, day) => <label key={label}><input type="checkbox" checked={schedule.weekdays.includes(day)} onChange={() => setSchedule((current) => ({ ...current, weekdays: current.weekdays.includes(day) ? current.weekdays.filter((value) => value !== day) : [...current.weekdays, day] }))} />{label}</label>)}</div></fieldset>}
      </>}
      <details className="task-form__wide"><summary>Workspace 与执行权限（可选）</summary>
        <div className="task-form__workspace">
          <label>Workspace<select value={useSessionWorkspace ? "session" : "none"} onChange={(event) => setUseSessionWorkspace(event.target.value === "session")}>
            <option value="session">{workspaceLoading ? "正在读取会话 Workspace…" : selectedSessionWorkspace?.displayPath ?? "当前会话未设置 Workspace"}</option>
            <option value="none">不使用 Workspace</option>
          </select></label>
          <div className="task-form__workspace-actions">
            <span>{selectedSessionWorkspace ? "默认使用所在会话的 Workspace。" : "选择目录后，会同时设为所在会话的 Workspace。"}</span>
            <button className="attach" type="button" onClick={() => void pickTaskWorkspace()} disabled={!sessionId || workspaceBusy}>{selectedSessionWorkspace ? "更换目录" : "选择目录"}</button>
          </div>
        </div>
        <fieldset disabled={!useSessionWorkspace || !selectedSessionWorkspace}><legend>自动执行权限</legend>{managedPermissionOptions.map(([value, label]) => <label className="checkbox-row" key={value}><input type="checkbox" checked={managedPermissions.includes(value)} onChange={() => setManagedPermissions((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value])} />{label}</label>)}</fieldset>
        <label>使用的 Skill（每行一个，最多 3 个）<textarea value={skillRefs} onChange={(event) => setSkillRefs(event.target.value)} rows={3} placeholder="输入 Skill ID" /></label>
      </details>
      <div className="task-form__footer task-form__wide"><span>{selectedSessionTitle ? `任务所在会话：${selectedSessionTitle}` : "请选择会话"}</span><button className="send" type="submit" disabled={busy || !sessionId || !instruction.trim()}>保存任务</button></div>
    </form>}
    <div className="scheduler-layout">
      <aside className="task-list" aria-label="定时任务列表"><div className="section-title"><p className="eyebrow">任务</p><span>{visibleTasks.length}</span></div><input className="task-list__search" value={taskQuery} onChange={(event) => setTaskQuery(event.target.value)} placeholder="搜索会话名称或任务内容" aria-label="搜索会话名称或任务内容" /><select className="task-list__filter" value={taskStatus} onChange={(event) => setTaskStatus(event.target.value)} aria-label="按任务状态筛选"><option value="all">全部状态</option><option value="active">等待执行</option><option value="completed">已完成</option><option value="failed">执行失败</option></select>{visibleTasks.length === 0 && <p className="muted">暂无匹配任务</p>}{visibleTasks.map((task) => <button className={`task-list__item ${task.taskId === selectedTaskId ? "task-list__item--active" : ""}`} type="button" key={task.taskId} onClick={() => void selectTask(task.taskId)}><strong>{task.instruction}</strong><small>{taskStatusLabels[task.status] ?? task.status} · {formatServerTime(task.nextRunAt)}</small></button>)}</aside>
      <section className="task-detail" aria-live="polite">
        {!selectedTask && <p className="empty">选择一个任务查看详情</p>}
        {selectedTask && <><div className="task-detail__top"><div><p className="eyebrow">{taskStatusLabels[selectedTask.status] ?? selectedTask.status}</p><h2>{selectedTask.instruction}</h2></div>{showArchived ? <div className="task-detail__actions"><button className="attach" type="button" onClick={() => void restoreTask()} disabled={busy}>恢复</button><button className="stop" type="button" onClick={() => void purgeTask()} disabled={busy}>永久删除</button></div> : <div className="task-detail__actions"><button className="attach" type="button" onClick={() => beginEdit(selectedTask)} disabled={busy}>编辑</button><button className="attach" type="button" onClick={() => void archiveTask()} disabled={busy}>归档</button></div>}</div>
          <dl className="task-meta"><div><dt>所在会话</dt><dd>{allSessions.find((session) => session.sessionId === selectedTask.sessionId)?.title ?? "会话不可用"}</dd></div><div><dt>下次执行时间</dt><dd>{formatServerTime(selectedTask.nextRunAt)}</dd></div><div><dt>执行计划</dt><dd>{describeSchedule(selectedTask.schedule)}</dd></div><div><dt>Workspace 与权限</dt><dd>{describeExecutionSettings(selectedTask, sessionWorkspaces[selectedTask.sessionId] ?? null)}</dd></div></dl>
          <section className="occurrence-history"><div className="section-title"><div><p className="eyebrow">执行记录</p><p className="muted">显示每次计划执行的结果</p></div><span>{occurrences.length}</span></div>{occurrences.length === 0 && <p className="muted">暂无执行记录</p>}{occurrences.map((occurrence) => <article className="occurrence" key={occurrence.occurrenceId}><div><strong>#{occurrence.sequenceNumber} · {occurrenceStatusLabels[occurrence.status] ?? occurrence.status}</strong><p>{formatServerTime(occurrence.scheduledFor)}{occurrence.missedCount > 0 ? ` · 合并 ${occurrence.missedCount} 次错过执行` : ""}</p>{occurrence.errorCode && <p className="occurrence__error">{occurrence.errorCode}</p>}</div>{!showArchived && occurrence.status === "failed" && <button className="steer" type="button" onClick={() => void retryOccurrence(occurrence.occurrenceId)} disabled={busy}>重试</button>}</article>)}{occurrenceCursor && <button className="attach occurrence-history__more" type="button" onClick={() => selectedTaskId && void loadOccurrences(selectedTaskId, occurrenceCursor)} disabled={busy}>加载更多</button>}</section>
        </>}
      </section>
    </div>
  </section>;
}

function scheduleBody(schedule: ScheduleDraft) {
  const base = { kind: schedule.kind, anchorAt: new Date(schedule.anchorAt).toISOString() };
  if (schedule.kind === "once") return base;
  if (schedule.kind === "interval") return { ...base, intervalSeconds: Number(schedule.intervalSeconds) };
  const calendar = { ...base, timezone: schedule.timezone, hour: Number(schedule.hour), minute: Number(schedule.minute) };
  return schedule.kind === "daily" ? calendar : { ...calendar, weekdays: schedule.weekdays };
}

function toDateTimeLocal(value: Date): string {
  const offset = value.getTimezoneOffset() * 60_000;
  return new Date(value.getTime() - offset).toISOString().slice(0, 16);
}

function formatServerTime(value: string | null): string {
  if (!value) return "无后续计划";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function describeSchedule(schedule: TaskSchedule): string {
  if (schedule.kind === "once") return "单次";
  if (schedule.kind === "interval") return `每 ${schedule.intervalSeconds ?? "?"} 秒`;
  const time = `${String(schedule.hour).padStart(2, "0")}:${String(schedule.minute).padStart(2, "0")} ${schedule.timezone ?? ""}`;
  if (schedule.kind === "daily") return `每天 ${time}`;
  return `每周 ${schedule.weekdays.map((day) => weekdays[day]).join("、")} ${time}`;
}

function describeExecutionSettings(task: SchedulerTask, workspace: WorkspaceBinding | null): string {
  const permissionLabels = new Map<string, string>(managedPermissionOptions);
  const values = [
    task.workspaceBindingRevision
      ? workspace?.revision === task.workspaceBindingRevision
        ? workspace.displayPath
        : "会话 Workspace 已变更"
      : null,
    task.managedPermissions.length ? task.managedPermissions.map((value) => permissionLabels.get(value) ?? value).join("、") : null,
    task.skillRefs.length ? `Skill：${task.skillRefs.map((item) => `${item.skillId} v${item.version}`).join("、")}` : null,
  ].filter(Boolean);
  return values.length ? values.join(" · ") : "未配置";
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : "本地请求失败";
}
