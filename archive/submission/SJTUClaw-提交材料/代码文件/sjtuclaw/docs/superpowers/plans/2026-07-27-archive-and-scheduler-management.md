# Archive and Scheduler Management Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add recoverable archive workflows for conversations and tasks, safe task editing, searchable task management, and a scrollable scheduler UI.

**Architecture:** Persist archive metadata on Session and Task. Default list queries exclude archived objects. SchedulerService owns task editing/archive lifecycle. ArchiveService coordinates conversation archive across RuntimeHost, SchedulerService, attachments/downloads, and SessionStore. React remains a Gateway client and filters loaded lists locally.

**Tech Stack:** Python 3.12, FastAPI/Pydantic, JSON stores, asyncio, React, TypeScript, Vite, pytest, mypy, ruff.

## Global Constraints

- Preserve the user’s uncommitted App.tsx and styles.css workspace-picker/settings changes.
- Session JSON writes schema v7 and reads v1-v6 as unarchived. Task JSON writes v3 and reads v1-v2 as unarchived.
- Normal lists exclude archived data. Permanent delete accepts archived objects only and requires confirmation.
- Scheduler edit/archive stops queued and active work before replacement scheduling.
- Do not create a worktree.

## File Structure

| File | Responsibility |
| --- | --- |
| models/session.py and models/scheduler.py | Archive fields, enums, invariants. |
| ports/stores.py and ports/scheduler.py | Normal/archive list and purge contracts. |
| infrastructure persistence files | Schema migration, filtering, scoped purge. |
| runtime/service.py | Stop one Session’s queued, running, and approval-paused requests. |
| runtime/scheduler.py | Task update, archive, restore, purge, session cascade. |
| runtime/archive_service.py | Session archive, restore, and purge coordination. |
| bootstrap.py and gateway.py | Wiring and protected HTTP API. |
| SchedulerView.tsx, App.tsx, styles.css | Scroll, task manager, archive interfaces. |

## Task 1: Archive domain model and persistence

**Files:**

- Modify: src/sjtuclaw/models/session.py
- Modify: src/sjtuclaw/models/scheduler.py
- Modify: src/sjtuclaw/ports/stores.py
- Modify: src/sjtuclaw/ports/scheduler.py
- Modify: src/sjtuclaw/infrastructure/persistence/json_store.py
- Modify: src/sjtuclaw/infrastructure/persistence/scheduler.py
- Test: tests/unit/test_json_store.py, tests/unit/test_scheduler_store.py, tests/contract/session_store_contract.py

**Interfaces produced:**

    SessionArchiveReason.USER_ARCHIVED
    TaskArchiveReason.USER_CANCELLED
    TaskArchiveReason.SESSION_ARCHIVED
    SessionStore.list(*, archived: bool = False)
    TaskStore.list_tasks(*, archived: bool = False)
    TaskStore.delete_task(task_id: str)

- [ ] Step 1: Write failing model and Store tests.

    def test_task_store_filters_archived_and_purges_occurrences(tmp_path: Path) -> None:
        store = JsonTaskStore(tmp_path)
        store.create_task(active_task)
        store.create_task(archived_task)
        store.create_occurrence(archived_occurrence)
        assert store.list_tasks() == (active_task,)
        assert store.list_tasks(archived=True) == (archived_task,)
        store.delete_task(archived_task.task_id)
        assert store.list_tasks(archived=True) == ()

- [ ] Step 2: Run python -m pytest -q tests/unit/test_json_store.py tests/unit/test_scheduler_store.py. Expect failure because fields, filters, and delete_task are absent.

- [ ] Step 3: Implement SessionArchiveReason and TaskArchiveReason; add paired UTC archived_at and archived_reason fields. Reject active archived Tasks and archived Tasks with next_run_at. Write Session v7 and Task v3. Supported old files load both archive fields as None. Filter list methods by archived. JsonTaskStore delete validates ID and deletes only the exact Task JSON and exact occurrence directory.

- [ ] Step 4: Run python -m pytest -q tests/unit/test_json_store.py tests/unit/test_scheduler_store.py tests/contract/session_store_contract.py. Expect PASS.

- [ ] Step 5: Commit with message feat: persist archived sessions and tasks.

## Task 2: Session-wide Runtime interruption

**Files:**

- Modify: src/sjtuclaw/runtime/service.py
- Modify: src/sjtuclaw/ports/runtime.py
- Test: tests/unit/test_runtime_host.py and tests/integration/test_gateway_runtime.py

**Interface produced:**

    async def stop_session(self, session_id: str) -> None

- [ ] Step 1: Write failing RuntimeHost coverage.

    async def test_stop_session_cancels_queued_started_and_waiting_approval() -> None:
        await host.submit(queued_request)
        await host.submit(started_request)
        await wait_for_pending_approval(host, session_id)
        await host.stop_session(session_id)
        stored = sessions.load(session_id)
        assert stored.pending_requests == ()
        assert stored.pending_approval is None

- [ ] Step 2: Run python -m pytest -q tests/unit/test_runtime_host.py -k stop_session. Expect failure because stop_session is undefined.

- [ ] Step 3: Implement stop_session through existing coordinator locks. It reloads Session state, calls cancel_queued for every pending request, and calls stop_run for every started run. Existing stop_run waiting-approval behavior clears the approval. Add method to runtime protocol and test fakes. Repeated calls are safe.

- [ ] Step 4: Run python -m pytest -q tests/unit/test_runtime_host.py tests/integration/test_gateway_runtime.py. Expect PASS.

- [ ] Step 5: Commit with message feat: stop all runs for archived sessions.

## Task 3: Scheduler task update and archive lifecycle

**Files:**

- Modify: src/sjtuclaw/runtime/scheduler.py
- Modify: src/sjtuclaw/ports/scheduler.py
- Test: tests/unit/test_scheduler_service.py and tests/integration/test_gateway_scheduler.py

**Interfaces produced:**

    async def update(self, task_id: str, *, session_id: str, instruction: str,
                     schedule: Schedule, workspace_binding_revision: int | None,
                     managed_permissions: tuple[str, ...],
                     skill_refs: tuple[SkillReference, ...]) -> Task
    async def archive(self, task_id: str, reason: TaskArchiveReason) -> Task
    def restore(self, task_id: str) -> Task
    def purge(self, task_id: str) -> None
    async def archive_for_session(self, session_id: str) -> tuple[Task, ...]

- [ ] Step 1: Write failing lifecycle tests.

    async def test_archive_restore_and_purge_task(tmp_path: Path) -> None:
        archived = await service.archive(task.task_id, TaskArchiveReason.USER_CANCELLED)
        assert archived.archived_at is not None
        assert service.restore(task.task_id).status is TaskStatus.CANCELLED
        await service.archive(task.task_id, TaskArchiveReason.USER_CANCELLED)
        service.purge(task.task_id)

- [ ] Step 2: Run python -m pytest -q tests/unit/test_scheduler_service.py tests/integration/test_gateway_scheduler.py. Expect failure because lifecycle methods and SchedulerRuntime.stop_run are absent.

- [ ] Step 3: Within SchedulerService lock clear next_run_at before interruption; cancel planned/queued Occurrences; call stop_run for running Occurrences; persist cancellation reasons CANCELLED_BY_TASK_EDIT, CANCELLED_BY_TASK_ARCHIVE, or CANCELLED_BY_SESSION_ARCHIVE. Save new configuration and calculate a fresh next run only after old work becomes terminal. Archive produces cancelled archived Task. Restore removes archive fields but retains cancelled. Purge rejects normal Task. All lifecycle changes wake the Scheduler.

- [ ] Step 4: Run python -m pytest -q tests/unit/test_scheduler_models.py tests/unit/test_scheduler_store.py tests/unit/test_scheduler_service.py tests/integration/test_gateway_scheduler.py. Expect PASS.

- [ ] Step 5: Commit with message feat: manage archived scheduler tasks.

## Task 4: ArchiveService session coordinator

**Files:**

- Create: src/sjtuclaw/runtime/archive_service.py
- Modify: src/sjtuclaw/runtime/__init__.py, src/sjtuclaw/runtime/session_service.py, src/sjtuclaw/bootstrap.py
- Modify attachment/download cleanup service or port only where required
- Create: tests/unit/test_archive_service.py
- Modify: tests/unit/test_session_service.py and tests/unit/test_attachment_service.py

**Interfaces produced:**

    class ArchiveService:
        async def archive_session(self, session_id: str) -> Session
        async def restore_session(self, session_id: str) -> Session
        async def purge_session(self, session_id: str) -> None

- [ ] Step 1: Write failing coordinator tests.

    async def test_archive_session_stops_runs_and_archives_its_tasks() -> None:
        archived = await archive_service.archive_session(session.session_id)
        assert archived.archived_reason is SessionArchiveReason.USER_ARCHIVED
        assert runtime.stopped_session_ids == [session.session_id]
        assert scheduler.archived_session_ids == [session.session_id]

- [ ] Step 2: Run python -m pytest -q tests/unit/test_archive_service.py. Expect failure because ArchiveService does not exist.

- [ ] Step 3: Implement ordering: take Session execution lock; load Session; await RuntimeHost.stop_session; await SchedulerService.archive_for_session; reload Session; save archived fields. Restore clears fields only. Purge verifies archived, purges archived Tasks first, cleans attachments/download snapshots, then deletes Session document. Wire service into RuntimeApplication and bootstrap.

- [ ] Step 4: Run python -m pytest -q tests/unit/test_archive_service.py tests/unit/test_session_service.py tests/unit/test_attachment_service.py. Expect PASS.

- [ ] Step 5: Commit with message feat: archive conversations with related tasks.

## Task 5: Gateway archive APIs

**Files:**

- Modify: src/sjtuclaw/entrypoints/gateway.py
- Create: tests/integration/test_gateway_archive.py
- Modify: tests/integration/test_gateway_runtime.py and tests/integration/test_gateway_scheduler.py

**Interfaces produced:**

    GET /sessions?archived=true|false
    POST /sessions/{id}/archive
    POST /sessions/{id}/restore
    DELETE /sessions/{id}
    GET /tasks?archived=true|false
    PUT /tasks/{id}
    POST /tasks/{id}/archive
    POST /tasks/{id}/restore
    DELETE /tasks/{id}

- [ ] Step 1: Write failing Gateway tests.

    def test_gateway_hides_archived_and_allows_archive_only_purge(client: TestClient) -> None:
        assert client.post(f"/api/v1/sessions/{session_id}/archive").status_code == 200
        assert session_id not in [item["sessionId"] for item in client.get("/api/v1/sessions").json()["sessions"]]
        assert client.delete(f"/api/v1/sessions/{session_id}").status_code == 204

- [ ] Step 2: Run python -m pytest -q tests/integration/test_gateway_archive.py. Expect failure because archive routes and payload fields are absent.

- [ ] Step 3: Add strict UpdateTaskBody, cookie/same-origin guarded routes, archived query flags, and archivedAt/archivedReason payloads. Delete returns 409 TASK_NOT_ARCHIVED or SESSION_NOT_ARCHIVED for normal data. Reject submit, attachment mutation, Workspace mutation, rename, and task creation for archived Session with 409 SESSION_ARCHIVED.

- [ ] Step 4: Run python -m pytest -q tests/integration/test_gateway_runtime.py tests/integration/test_gateway_scheduler.py tests/integration/test_gateway_archive.py. Expect PASS.

- [ ] Step 5: Commit with message feat: expose archive management APIs.

## Task 6: SchedulerView normal/archive manager

**Files:**

- Modify: web/src/SchedulerView.tsx
- Modify: web/src/styles.css

**Interfaces produced:**

    type SkillReference = {
      skillId: string; snapshotId: string; snapshotHash: string; version: string;
    }
    function matchesTask(task: SchedulerTask, title: string,
                         query: string, status: string): boolean

- [ ] Step 1: Add archive fields and typed SkillReference to Task payload. Filter only normalized Session title plus instruction, combined with selected status.

- [ ] Step 2: Load normal or archive list using archived query. Add labelled search/status controls, edit form prefill, archive confirmation, restore, and archive-only permanent-purge confirmation. Render skill label as skillId plus version.

- [ ] Step 3: Make scheduler page 100dvh flex with overflow-y auto and independent wide-screen task-list scrolling. Keep responsive single-column behavior.

- [ ] Step 4: Run npm run build. Manually verify tall form scrolls to list; search, status filters, edit, archive, restore, and purge refresh selection.

- [ ] Step 5: Commit with message feat: manage archived scheduler tasks in UI.

## Task 7: Archived conversation UI and dirty-change-safe merge

**Files:**

- Modify: web/src/App.tsx
- Modify: web/src/styles.css

- [ ] Step 1: Run git diff -- web/src/App.tsx web/src/styles.css and preserve existing workspace-picker/settings-launcher edits. Do not reset, checkout, stash, or bulk-rewrite.

- [ ] Step 2: Add Session archive fields and UI state.

    type SessionSummary = {
      sessionId: string; title: string;
      archivedAt: string | null; archivedReason: string | null;
    }
    const [showArchivedSessions, setShowArchivedSessions] = useState(false)
    const [archiveQuery, setArchiveQuery] = useState("")

Load normal Sessions with archived=false. Load archive list only after entering archive view. Disable composer, attachments, Workspace, rename, and task creation for archived selection.

- [ ] Step 3: Change single/bulk delete labels and handlers to archive. Add archived conversation view with title-only search, read-only details, restore, and separately confirmed permanent delete. After archive choose next normal Session or empty state. Do not auto-restore related Tasks.

- [ ] Step 4: Run npm run build. Manually archive a Session with a Task; verify normal search hides Session and archived tasks contains Task. Restore Session and verify messages return while Task remains archived. Verify permanent delete exists only in archive view.

- [ ] Step 5: Review git diff for App.tsx/styles.css. If pre-existing user changes would be committed together, ask user whether to include them or leave all frontend modifications uncommitted. Never discard user changes.

## Task 8: Documentation and final verification

**Files:**

- Modify: project_structure.md
- Modify: README.md only if archive navigation/API is already documented there

- [ ] Step 1: Add runtime/archive_service.py to project structure and explain archive-aware SchedulerView/Gateway responsibilities.

- [ ] Step 2: Run complete verification.

    python -m pytest -q
    python -m mypy src
    python -m ruff check .
    python -m compileall -q src
    cd web
    npm run build
    cd ..
    git diff --check
    git status --short

Expected: every command exits 0, no whitespace errors, and remaining uncommitted files are intentional.

- [ ] Step 3: Commit documentation only if frontend commit scope remains unresolved. Do not include App.tsx or styles.css without explicit user authorization for their pre-existing changes.

## Plan Self-Review

- Spec coverage: Tasks 1-5 implement archive data, interruption, task/session lifecycle, and APIs. Tasks 6-7 implement scrolling, searches, filtering, task management, and archived Sessions. Task 8 documents and verifies all requirements.
- Placeholder scan: No task contains TBD, TODO, or unspecified future work.
- Type consistency: archived_at/archivedAt, archived_reason/archivedReason, ArchiveService, SchedulerService update/archive/restore/purge, and RuntimeHost stop_session use identical names throughout.

