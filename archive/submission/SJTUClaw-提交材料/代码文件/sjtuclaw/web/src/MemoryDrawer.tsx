import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import type { ApiClient } from "./SchedulerView";
import {
  categoryLabel,
  eventTimePayload,
  formatMemoryTime,
  formatRelativeMemoryTime,
  groupedMemories,
  memoryCategories,
  memoryMoment,
  mergeMemories,
  toDateTimeLocal,
  type MemoryCategory,
  type MemoryEntry,
  type MemoryPage,
} from "./memory";

type MemoryDraft = {
  content: string;
  category: MemoryCategory;
  eventAt: string;
};

type MemoryPolicySnapshot = {
  rules: string;
  defaultRules: string;
  isOverride: boolean;
  revision: number;
  updatedAt: string | null;
};

type MemoryHealth = "ready" | "unavailable" | "error";

type MemoryDrawerProps = {
  api: ApiClient;
  open: boolean;
  refreshToken: number;
  onClose: () => void;
  memoryHealth: MemoryHealth;
  onMemoryUnavailable: () => void;
  onRetryHealth: () => Promise<void>;
};

const emptyDraft = (): MemoryDraft => ({ content: "", category: "other", eventAt: "" });

export function MemoryDrawer({
  api,
  open,
  refreshToken,
  onClose,
  memoryHealth,
  onMemoryUnavailable,
  onRetryHealth,
}: MemoryDrawerProps) {
  const [activeTab, setActiveTab] = useState<"timeline" | "rules">("timeline");
  const [items, setItems] = useState<MemoryEntry[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<MemoryEntry | null>(null);
  const [draft, setDraft] = useState<MemoryDraft>(emptyDraft);
  const [saving, setSaving] = useState(false);
  const [adding, setAdding] = useState(false);
  const [createDraft, setCreateDraft] = useState<MemoryDraft>(emptyDraft);
  const [creating, setCreating] = useState(false);
  const [conflictNotice, setConflictNotice] = useState<string | null>(null);
  const [policy, setPolicy] = useState<MemoryPolicySnapshot | null>(null);
  const [policyDraft, setPolicyDraft] = useState("");
  const [policyLoading, setPolicyLoading] = useState(false);
  const [policySaving, setPolicySaving] = useState(false);
  const [policyError, setPolicyError] = useState<string | null>(null);
  const [resetPreviewOpen, setResetPreviewOpen] = useState(false);
  const drawer = useRef<HTMLElement>(null);
  const abortRequest = useRef<AbortController | null>(null);
  const requestGeneration = useRef(0);
  const policyGeneration = useRef(0);

  const loadPage = useCallback(async (cursor: string | null, replace: boolean) => {
    const generation = ++requestGeneration.current;
    abortRequest.current?.abort();
    const controller = new AbortController();
    abortRequest.current = controller;
    if (replace) setLoading(true);
    else setLoadingMore(true);
    if (replace) setError(null);
    try {
      const params = new URLSearchParams({ limit: "50" });
      if (query.trim()) params.set("query", query.trim());
      if (cursor) params.set("cursor", cursor);
      const page = await api<MemoryPage>(`/memories?${params.toString()}`, { signal: controller.signal });
      if (generation !== requestGeneration.current) return;
      setItems((current) => replace ? mergeMemories([], page.items) : mergeMemories(current, page.items));
      setNextCursor(page.nextCursor);
      return page;
    } catch (caught) {
      if (!isAborted(caught) && generation === requestGeneration.current) {
        if (isMemoryUnavailable(caught)) onMemoryUnavailable();
        setError(messageOf(caught));
      }
      return null;
    } finally {
      if (generation === requestGeneration.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, [api, onMemoryUnavailable, query]);

  const loadPolicy = useCallback(async (syncDraft: boolean) => {
    const generation = ++policyGeneration.current;
    setPolicyLoading(true);
    setPolicyError(null);
    try {
      const snapshot = await api<MemoryPolicySnapshot>("/memory-policy");
      if (generation !== policyGeneration.current) return null;
      setPolicy(snapshot);
      if (syncDraft) setPolicyDraft(snapshot.rules);
      return snapshot;
    } catch (caught) {
      if (generation !== policyGeneration.current) return null;
      if (isMemoryUnavailable(caught)) onMemoryUnavailable();
      setPolicyError(messageOf(caught));
      return null;
    } finally {
      if (generation === policyGeneration.current) setPolicyLoading(false);
    }
  }, [api, onMemoryUnavailable]);

  useEffect(() => {
    if (open) return;
    abortRequest.current?.abort();
    setQuery("");
    setEditing(null);
    setAdding(false);
    setConflictNotice(null);
    setActiveTab("timeline");
    setResetPreviewOpen(false);
    policyGeneration.current += 1;
    setPolicy(null);
    setPolicyDraft("");
    setPolicyLoading(false);
    setPolicySaving(false);
    setPolicyError(null);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    void loadPage(null, true);
    return () => abortRequest.current?.abort();
  }, [loadPage, open, refreshToken]);

  useEffect(() => {
    if (!open) return;
    const panel = drawer.current;
    window.requestAnimationFrame(() => panel?.querySelector<HTMLButtonElement>(".memory-drawer__close")?.focus());
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose, open]);

  const selectTab = (tab: "timeline" | "rules") => {
    setActiveTab(tab);
    if (tab === "rules") void loadPolicy(policy === null);
  };

  const refreshAfterConflict = async (memoryId?: string) => {
    setConflictNotice("记忆已变化，请核对后重试。");
    const page = await loadPage(null, true);
    const refreshed = page?.items.find((item) => item.memoryId === memoryId);
    if (refreshed) setEditing((current) => current?.memoryId === refreshed.memoryId ? refreshed : current);
  };

  const beginEdit = (memory: MemoryEntry) => {
    setEditing(memory);
    setDraft({ content: memory.content, category: memory.category, eventAt: toDateTimeLocal(memory.eventAt) });
    setConflictNotice(null);
  };

  const updateMemory = async (event: FormEvent) => {
    event.preventDefault();
    if (!editing || !draft.content.trim() || saving) return;
    setSaving(true);
    setConflictNotice(null);
    try {
      const response = await api<MemoryEntry>(`/memories/${encodeURIComponent(editing.memoryId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: draft.content.trim(),
          category: draft.category,
          originalTimeText: editing.originalTimeText,
          ...eventTimePayload(draft.eventAt),
          revision: editing.revision,
        }),
      });
      setItems((current) => mergeMemories(current.filter((item) => item.memoryId !== response.memoryId), [response]));
      setEditing(null);
    } catch (caught) {
      if (isConflict(caught)) await refreshAfterConflict(editing.memoryId);
      else {
        if (isMemoryUnavailable(caught)) onMemoryUnavailable();
        setError(messageOf(caught));
      }
    } finally {
      setSaving(false);
    }
  };

  const deleteMemory = async (memory: MemoryEntry) => {
    if (!window.confirm("永久删除这条记忆？此操作不能恢复。")) return;
    setError(null);
    try {
      await api<void>(`/memories/${encodeURIComponent(memory.memoryId)}?revision=${memory.revision}`, { method: "DELETE" });
      if (editing?.memoryId === memory.memoryId) setEditing(null);
      if (nextCursor) await loadPage(null, true);
      else setItems((current) => current.filter((item) => item.memoryId !== memory.memoryId));
    } catch (caught) {
      if (isConflict(caught)) await refreshAfterConflict(memory.memoryId);
      else {
        if (isMemoryUnavailable(caught)) onMemoryUnavailable();
        setError(messageOf(caught));
      }
    }
  };

  const createMemory = async (event: FormEvent) => {
    event.preventDefault();
    if (!createDraft.content.trim() || creating) return;
    setCreating(true);
    setError(null);
    try {
      const response = await api<MemoryEntry>("/memories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: createDraft.content.trim(),
          category: createDraft.category,
          ...eventTimePayload(createDraft.eventAt),
        }),
      });
      setItems((current) => mergeMemories(current, [response]));
      setCreateDraft(emptyDraft());
      setAdding(false);
    } catch (caught) {
      if (isMemoryUnavailable(caught)) onMemoryUnavailable();
      setError(messageOf(caught));
    } finally {
      setCreating(false);
    }
  };

  const savePolicy = async (event: FormEvent) => {
    event.preventDefault();
    if (!policy || !policyDraft.trim() || policySaving) return;
    const generation = ++policyGeneration.current;
    setPolicySaving(true);
    setPolicyError(null);
    try {
      const snapshot = await api<MemoryPolicySnapshot>("/memory-policy", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rules: policyDraft, revision: policy.revision }),
      });
      if (generation !== policyGeneration.current) return;
      setPolicy(snapshot);
      setPolicyDraft(snapshot.rules);
      setResetPreviewOpen(false);
    } catch (caught) {
      if (generation !== policyGeneration.current) return;
      if (isMemoryUnavailable(caught)) onMemoryUnavailable();
      setPolicyError(messageOf(caught));
    } finally {
      if (generation === policyGeneration.current) setPolicySaving(false);
    }
  };

  const resetPolicy = async () => {
    if (!policy || policySaving) return;
    const generation = ++policyGeneration.current;
    setPolicySaving(true);
    setPolicyError(null);
    try {
      const snapshot = await api<MemoryPolicySnapshot>("/memory-policy/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision: policy.revision }),
      });
      if (generation !== policyGeneration.current) return;
      setPolicy(snapshot);
      setPolicyDraft(snapshot.rules);
      setResetPreviewOpen(false);
    } catch (caught) {
      if (generation !== policyGeneration.current) return;
      if (isMemoryUnavailable(caught)) onMemoryUnavailable();
      setPolicyError(messageOf(caught));
    } finally {
      if (generation === policyGeneration.current) setPolicySaving(false);
    }
  };

  const retry = async () => {
    await onRetryHealth();
    if (activeTab === "timeline") await loadPage(null, true);
    else await loadPolicy(policy === null);
  };

  const groups = groupedMemories(items);

  return <aside
    ref={drawer}
    id="memory-drawer"
    className={`memory-drawer ${open ? "memory-drawer--open" : ""}`}
    role="dialog"
    aria-modal="false"
    aria-hidden={!open}
    aria-label="记忆"
  >
    <header className="memory-drawer__header">
      <div><p className="eyebrow">长期记忆</p><h2>{activeTab === "timeline" ? "记忆时间线" : "记忆规则"}</h2></div>
      <button className="memory-drawer__close" type="button" onClick={onClose} aria-label="关闭记忆">×</button>
    </header>
    <div className="memory-tabs" role="tablist" aria-label="记忆视图">
      <button id="memory-tab-timeline" type="button" role="tab" aria-selected={activeTab === "timeline"} aria-controls="memory-panel-timeline" onClick={() => selectTab("timeline")}>时间线</button>
      <button id="memory-tab-rules" type="button" role="tab" aria-selected={activeTab === "rules"} aria-controls="memory-panel-rules" onClick={() => selectTab("rules")}>记忆规则</button>
    </div>

    {memoryHealth !== "ready" && <section className="memory-error memory-error--persistent" role="alert">
      <p>{memoryHealth === "unavailable" ? "记忆存储暂时不可用。聊天和定时任务不受影响；修复存储后可重试。" : "暂时无法连接记忆服务。请检查本地 Gateway 后重试。"}</p>
      <button type="button" onClick={() => void retry()}>重试</button>
    </section>}

    {activeTab === "timeline" && <section id="memory-panel-timeline" className="memory-tab-panel" role="tabpanel" aria-labelledby="memory-tab-timeline">
    <div className="memory-drawer__tools">
      <label className="visually-hidden" htmlFor="memory-search">搜索记忆</label>
      <input
        id="memory-search"
        value={query}
        onChange={(event) => {
          abortRequest.current?.abort();
          requestGeneration.current += 1;
          setItems([]);
          setNextCursor(null);
          setQuery(event.target.value);
        }}
        placeholder="搜索记忆"
        aria-label="搜索记忆"
      />
      <button
        className="memory-drawer__add"
        type="button"
        aria-expanded={adding}
        onClick={() => setAdding((current) => !current)}
      >+ 添加记忆</button>
    </div>

    {adding && <form className="memory-form memory-form--create" onSubmit={(event) => void createMemory(event)}>
      <label>记忆内容<textarea value={createDraft.content} onChange={(event) => setCreateDraft((current) => ({ ...current, content: event.target.value }))} rows={3} maxLength={4000} required autoFocus /></label>
      <label>分类<select value={createDraft.category} onChange={(event) => setCreateDraft((current) => ({ ...current, category: event.target.value as MemoryCategory }))}>{memoryCategories.map((category) => <option key={category.value} value={category.value}>{category.label}</option>)}</select></label>
      <label>事件时间（可选）<input type="datetime-local" value={createDraft.eventAt} onChange={(event) => setCreateDraft((current) => ({ ...current, eventAt: event.target.value }))} /></label>
      <div className="memory-form__actions"><button type="button" onClick={() => { setAdding(false); setCreateDraft(emptyDraft()); }} disabled={creating}>取消</button><button className="send" type="submit" disabled={creating || !createDraft.content.trim()}>{creating ? "正在添加…" : "添加记忆"}</button></div>
    </form>}

    {conflictNotice && <p className="memory-notice" role="status">{conflictNotice}</p>}
    {error && <section className="memory-error" role="alert"><p>{error}</p><button type="button" onClick={() => void loadPage(null, true)}>重试</button></section>}
    {loading && <p className="memory-state" role="status">正在加载记忆…</p>}
    {!loading && !error && groups.length === 0 && <p className="memory-state">还没有匹配的记忆。可手动添加，或在后续对话中让系统静默记录有用信息。</p>}
    {!loading && groups.length > 0 && <div className="memory-timeline" aria-live="polite">
      {groups.map((group) => <section className="memory-timeline__group" key={group.label}>
        <h3>{group.label}</h3>
        {group.items.map((memory) => {
          const moment = memoryMoment(memory);
          const isEditing = editing?.memoryId === memory.memoryId;
          return <article className="memory-node" key={memory.memoryId}>
            <span className={`memory-node__dot memory-node__dot--${memory.category}`} aria-hidden="true" />
            <div className="memory-node__card">
              <div className="memory-node__meta"><span className={`memory-category memory-category--${memory.category}`}>{categoryLabel(memory.category)}</span><span>{memory.sourceKind === "manual" ? "手动" : "自动"}</span></div>
              {isEditing ? <form className="memory-form" onSubmit={(event) => void updateMemory(event)}>
                <label>记忆内容<textarea value={draft.content} onChange={(event) => setDraft((current) => ({ ...current, content: event.target.value }))} rows={3} maxLength={4000} required /></label>
                <label>分类<select value={draft.category} onChange={(event) => setDraft((current) => ({ ...current, category: event.target.value as MemoryCategory }))}>{memoryCategories.map((category) => <option key={category.value} value={category.value}>{category.label}</option>)}</select></label>
                <label>事件时间（可选）<input type="datetime-local" value={draft.eventAt} onChange={(event) => setDraft((current) => ({ ...current, eventAt: event.target.value }))} /></label>
                <div className="memory-form__actions"><button type="button" onClick={() => setEditing(null)} disabled={saving}>取消</button><button className="send" type="submit" disabled={saving || !draft.content.trim()}>{saving ? "正在保存…" : "保存"}</button></div>
              </form> : <>
                <p className="memory-node__content">{memory.content}</p>
                <p className="memory-node__time"><strong>{memory.eventAt ? "事件时间" : "记录时间"}</strong> {formatRelativeMemoryTime(moment)} · {formatMemoryTime(moment)}</p>
                {memory.originalTimeText && <p className="memory-node__original">原始时间描述：{memory.originalTimeText}</p>}
                {memory.eventAt && memory.eventTimezone && <p className="memory-node__original">时区：{memory.eventTimezone}</p>}
                <div className="memory-node__actions"><button type="button" onClick={() => beginEdit(memory)}>编辑</button><button className="memory-node__delete" type="button" onClick={() => void deleteMemory(memory)}>删除</button></div>
              </>}
            </div>
          </article>;
        })}
      </section>)}
      {nextCursor && <button className="memory-drawer__more" type="button" onClick={() => void loadPage(nextCursor, false)} disabled={loadingMore}>{loadingMore ? "正在加载…" : "加载更多"}</button>}
    </div>}
    </section>}

    {activeTab === "rules" && <section id="memory-panel-rules" className="memory-rules" role="tabpanel" aria-labelledby="memory-tab-rules">
      {policyLoading && <p className="memory-state" role="status">正在加载记忆规则…</p>}
      {policyError && <section className="memory-error" role="alert"><p>{policyError}</p><button type="button" onClick={() => void loadPolicy(policy === null)}>重试</button></section>}
      {policy && !policyLoading && <>
        <div className="memory-rules__meta">
          <span className={policy.isOverride ? "memory-rules__override" : ""}>{policy.isOverride ? "正在使用自定义规则" : "正在使用默认规则"}</span>
          <span>修订 {policy.revision}</span>
          {policy.updatedAt && <span>更新于 {formatMemoryTime(policy.updatedAt)}</span>}
        </div>
        <form className="memory-form memory-rules__form" onSubmit={(event) => void savePolicy(event)}>
          <label htmlFor="memory-rules-text">用于后续自动记忆的规则
            <textarea id="memory-rules-text" value={policyDraft} onChange={(event) => setPolicyDraft(event.target.value)} rows={12} maxLength={12000} required disabled={policySaving} />
          </label>
          <p className="memory-rules__hint">规则只影响之后的自动提取，不会重写已有记忆。</p>
          <div className="memory-form__actions">
            <button type="button" onClick={() => setResetPreviewOpen((current) => !current)} disabled={policySaving}>恢复默认</button>
            <button className="send" type="submit" disabled={policySaving || !policyDraft.trim()}>{policySaving ? "正在保存…" : "保存规则"}</button>
          </div>
        </form>
        {resetPreviewOpen && <section className="memory-reset-preview" aria-label="默认规则预览">
          <p>恢复后，自定义规则会被删除；已有记忆不变。</p>
          <pre>{policy.defaultRules}</pre>
          <div className="memory-form__actions">
            <button type="button" onClick={() => setResetPreviewOpen(false)} disabled={policySaving}>取消</button>
            <button className="memory-reset-preview__confirm" type="button" onClick={() => void resetPolicy()} disabled={policySaving}>{policySaving ? "正在恢复…" : "确认恢复默认规则"}</button>
          </div>
        </section>}
      </>}
    </section>}
  </aside>;
}

function isAborted(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function isConflict(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error
    && (error as { code?: string }).code === "MEMORY_REVISION_CONFLICT";
}

function isMemoryUnavailable(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error
    && (error as { code?: string }).code === "MEMORY_STORE_UNAVAILABLE";
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : "本地请求失败";
}
