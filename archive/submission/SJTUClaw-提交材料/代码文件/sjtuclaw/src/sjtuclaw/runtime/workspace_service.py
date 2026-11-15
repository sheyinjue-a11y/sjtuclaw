"""Session-scoped Workspace binding with admission-safe replacement."""

from dataclasses import replace
from pathlib import Path

from sjtuclaw.errors import SJTUClawError
from sjtuclaw.models.session import RunStatus, Session, WorkspaceBinding
from sjtuclaw.ports.runtime import Clock, IdGenerator
from sjtuclaw.ports.stores import SessionStore
from sjtuclaw.runtime.session_coordinator import SessionCoordinator
from sjtuclaw.runtime.session_state import require_session_mutable


class WorkspaceServiceError(SJTUClawError):
    """A stable Workspace operation failure that never reveals local paths."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WorkspaceService:
    """Bind exactly one resolved directory to a Session at a time."""

    def __init__(
        self,
        *,
        sessions: SessionStore,
        coordinator: SessionCoordinator,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._sessions = sessions
        self._coordinator = coordinator
        self._clock = clock
        self._ids = ids

    async def bind(self, session_id: str, root: str | Path) -> WorkspaceBinding:
        """Create a first binding; callers must use replace for an existing one."""

        return await self._set_binding(session_id, root, require_existing=False)

    async def replace(self, session_id: str, root: str | Path) -> WorkspaceBinding:
        """Replace an existing binding while retaining its stable binding ID."""

        return await self._set_binding(session_id, root, require_existing=True)

    async def unbind(self, session_id: str) -> None:
        """Remove the current binding after the same admission check as a replace."""

        async with self._coordinator.execution_lock(session_id):
            async with self._coordinator.admission_lock(session_id):
                session = self._sessions.load(session_id)
                require_session_mutable(session)
                self._require_workspace_idle(session)
                if session.workspace_binding is None:
                    return
                self._sessions.save(
                    replace(
                        session,
                        workspace_binding=None,
                        updated_at=self._clock.now(),
                    )
                )

    def binding_for(self, session_id: str) -> WorkspaceBinding | None:
        """Return a usable binding, never silently falling back from an invalid root."""

        binding = self._sessions.load(session_id).workspace_binding
        if binding is None:
            return None
        resolved = _resolve_existing_directory(binding.canonical_root)
        if str(resolved) != binding.canonical_root:
            raise WorkspaceServiceError("WORKSPACE_UNAVAILABLE")
        return binding

    async def _set_binding(
        self,
        session_id: str,
        root: str | Path,
        *,
        require_existing: bool,
    ) -> WorkspaceBinding:
        resolved = _resolve_existing_directory(root)
        display_path = _display_path(root)
        async with self._coordinator.execution_lock(session_id):
            async with self._coordinator.admission_lock(session_id):
                session = self._sessions.load(session_id)
                require_session_mutable(session)
                self._require_workspace_idle(session)
                previous = session.workspace_binding
                if require_existing and previous is None:
                    raise WorkspaceServiceError("WORKSPACE_NOT_CONFIGURED")
                if not require_existing and previous is not None:
                    raise WorkspaceServiceError("WORKSPACE_ALREADY_CONFIGURED")
                binding = WorkspaceBinding(
                    binding_id=(
                        previous.binding_id
                        if previous is not None
                        else self._ids.new_id("workspace")
                    ),
                    canonical_root=str(resolved),
                    display_path=display_path,
                    revision=previous.revision + 1 if previous is not None else 1,
                    bound_at=self._clock.now(),
                )
                self._sessions.save(
                    replace(
                        session,
                        workspace_binding=binding,
                        updated_at=self._clock.now(),
                    )
                )
                return binding

    @staticmethod
    def _require_workspace_idle(session: Session) -> None:
        if session.pending_requests or session.pending_approval is not None or any(
            run.status in {RunStatus.QUEUED, RunStatus.STARTED} for run in session.runs
        ):
            raise WorkspaceServiceError("WORKSPACE_BUSY")


def _resolve_existing_directory(root: str | Path) -> Path:
    if isinstance(root, str) and not root.strip():
        raise WorkspaceServiceError("WORKSPACE_UNAVAILABLE")
    try:
        resolved = Path(root).resolve(strict=True)
        if not resolved.is_dir():
            raise OSError("workspace root is not a directory")
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceServiceError("WORKSPACE_UNAVAILABLE") from exc
    return resolved


def _display_path(root: str | Path) -> str:
    display_path = str(root).strip()
    if not display_path:
        raise WorkspaceServiceError("WORKSPACE_UNAVAILABLE")
    return display_path
