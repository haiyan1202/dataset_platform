from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import ImportBatch, OperationHistory, Sample


def record_operation(
    db: Session,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None,
    dataset_id: uuid.UUID,
    action: str,
    summary: str,
    payload: dict,
) -> OperationHistory:
    """Append an undoable operation and discard the old redo branch."""
    db.execute(
        update(OperationHistory)
        .where(
            OperationHistory.organization_id == organization_id,
            OperationHistory.dataset_id == dataset_id,
            OperationHistory.status == "undone",
        )
        .values(status="discarded")
    )
    record = OperationHistory(
        organization_id=organization_id,
        user_id=user_id,
        dataset_id=dataset_id,
        action=action,
        summary=summary,
        payload_json=payload,
        status="applied",
        created_at=datetime.now(timezone.utc),
    )
    db.add(record)
    db.flush()
    return record


def _apply_sample_subset(db: Session, payload: dict, *, forward: bool) -> None:
    state_key = "after" if forward else "before"
    for item in payload.get("samples", []):
        sample = db.get(Sample, uuid.UUID(item["id"]))
        if sample is not None:
            sample.subset = item[state_key].get("subset")


def _apply_sample_delete(db: Session, payload: dict, *, forward: bool) -> None:
    for item in payload.get("samples", []):
        sample = db.get(Sample, uuid.UUID(item["id"]))
        if sample is None:
            continue
        if forward:
            sample.deleted_at = datetime.now(timezone.utc)
            sample.status = "deleted"
        else:
            sample.deleted_at = None
            sample.status = item.get("before", {}).get("status", "ready")


def _apply_batch_update(db: Session, payload: dict, *, forward: bool) -> None:
    batch = db.get(ImportBatch, uuid.UUID(payload["batch_id"]))
    if batch is None:
        return
    values = payload["after"] if forward else payload["before"]
    batch.batch_name = values["batch_name"]
    batch.note = values.get("note")


def _apply_batch_delete(db: Session, payload: dict, *, forward: bool) -> None:
    batch = db.get(ImportBatch, uuid.UUID(payload["batch_id"]))
    if batch is not None:
        if forward:
            batch.deleted_at = datetime.now(timezone.utc)
            batch.status = "deleted"
        else:
            batch.deleted_at = None
            batch.status = payload.get("before", {}).get("status", "ready")
    for item in payload.get("samples", []):
        sample = db.get(Sample, uuid.UUID(item["id"]))
        if sample is None:
            continue
        if forward:
            sample.deleted_at = datetime.now(timezone.utc)
            sample.status = "deleted"
        else:
            sample.deleted_at = None
            sample.status = item.get("before", {}).get("status", "ready")


def apply_operation(db: Session, record: OperationHistory, *, forward: bool) -> None:
    if record.action == "samples.subset_update":
        _apply_sample_subset(db, record.payload_json, forward=forward)
    elif record.action == "samples.delete":
        _apply_sample_delete(db, record.payload_json, forward=forward)
    elif record.action == "import_batch.update":
        _apply_batch_update(db, record.payload_json, forward=forward)
    elif record.action == "import_batch.delete":
        _apply_batch_delete(db, record.payload_json, forward=forward)
    else:
        raise ValueError(f"Operation {record.action!r} cannot be replayed")


def undo_operation(db: Session, record: OperationHistory) -> OperationHistory:
    if record.status != "applied":
        raise ValueError("history.operation_not_applied")
    apply_operation(db, record, forward=False)
    record.status = "undone"
    return record


def redo_operation(db: Session, record: OperationHistory) -> OperationHistory:
    if record.status != "undone":
        raise ValueError("history.operation_not_undone")
    apply_operation(db, record, forward=True)
    record.status = "applied"
    return record