"""Merge persisted queue state into a session snapshot held by an active run."""

from dataclasses import replace

from sjtuclaw.errors import SJTUClawError
from sjtuclaw.models.run_record import RunRecord
from sjtuclaw.models.session import RunState, Session


class SessionArchivedError(SJTUClawError):
    """A Session mutation was rejected after its archive transition."""

    code = "SESSION_ARCHIVED"

    def __init__(self) -> None:
        super().__init__(self.code)


def require_session_mutable(session: Session) -> None:
    if session.archived_at is not None:
        raise SessionArchivedError


def merge_active_session(current: Session, updated: Session) -> Session:
    """Keep queued work accepted after an active run loaded its local snapshot."""

    if current.session_id != updated.session_id:
        raise ValueError("cannot merge different sessions")
    run_records = _merge_records(current.run_records, updated.run_records)
    runs = _merge_runs(current.runs, updated.runs)
    return replace(
        updated,
        pending_requests=current.pending_requests,
        runs=runs,
        run_records=run_records,
        archived_at=current.archived_at,
        archived_reason=current.archived_reason,
        updated_at=max(current.updated_at, updated.updated_at),
        next_sequence_number=max(
            current.next_sequence_number,
            updated.next_sequence_number,
        ),
    )


def _merge_runs(
    current: tuple[RunState, ...],
    updated: tuple[RunState, ...],
) -> tuple[RunState, ...]:
    updated_ids = {run.run_id for run in updated}
    return (*updated, *(run for run in current if run.run_id not in updated_ids))


def _merge_records(
    current: tuple[RunRecord, ...],
    updated: tuple[RunRecord, ...],
) -> tuple[RunRecord, ...]:
    records = {record.record_id: record for record in current}
    records.update({record.record_id: record for record in updated})
    return tuple(sorted(records.values(), key=lambda record: record.sequence_number))
