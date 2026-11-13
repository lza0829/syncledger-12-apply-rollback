from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import apply_service as svc

router = APIRouter(tags=["apply"])


class ApplyBody(BaseModel):
    diff_id: int
    direction: str  # left_to_right | right_to_left


class RollbackBody(BaseModel):
    apply_ids: list[int]
    note: str | None = None


@router.get("/apply/records")
def list_records(pair_id: int | None = None, status: str | None = None):
    return svc.list_records(pair_id=pair_id, status=status)


@router.get("/apply/events")
def list_events(pair_id: int | None = None, limit: int = 300):
    return svc.list_events(pair_id=pair_id, limit=limit)


@router.get("/apply/stats")
def apply_stats(pair_id: int | None = None):
    return svc.apply_stats(pair_id=pair_id)


@router.get("/apply/side-values")
def list_side_values(pair_id: int, side: str | None = None):
    return svc.list_side_values(pair_id=pair_id, side=side)


@router.get("/apply/rollback-batches")
def list_rollback_batches():
    return svc.list_rollback_batches()


@router.post("/apply")
def apply(body: ApplyBody):
    try:
        return svc.apply_diff(body.diff_id, body.direction)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/apply/rollback")
def rollback(body: RollbackBody):
    try:
        return svc.rollback_batch(body.apply_ids, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
