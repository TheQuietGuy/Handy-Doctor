"""
app/journal/router.py - Symptom journal endpoints.

  GET    /api/journal              list entries (newest first), ?days=N&symptom=X
  POST   /api/journal              create an entry
  PATCH  /api/journal/{id}         edit an entry
  DELETE /api/journal/{id}         delete an entry
  GET    /api/journal/summary      per-symptom stats, trends and a daily series
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_verified_user
from app.database import get_db
from app.journal.models import SymptomEntry
from app.journal.schemas import (
    DailyPoint,
    EntryCreate,
    EntryListOut,
    EntryOut,
    EntryUpdate,
    JournalSummaryOut,
    SymptomStat,
)
from app.models import User

router = APIRouter(prefix="/api/journal", tags=["journal"])

CurrentUser = Annotated[User, Depends(get_verified_user)]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _get_owned(db: AsyncSession, user: User, entry_id: uuid.UUID) -> SymptomEntry:
    entry = await db.get(SymptomEntry, entry_id)
    if entry is None or entry.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found.")
    return entry


@router.get("", response_model=EntryListOut)
async def list_entries(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=30, ge=1, le=365),
    symptom: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=200, ge=1, le=500),
) -> EntryListOut:
    since = datetime.now(UTC) - timedelta(days=days)
    q = select(SymptomEntry).where(
        SymptomEntry.user_id == user.id, SymptomEntry.recorded_at >= since
    )
    if symptom:
        q = q.where(func.lower(SymptomEntry.symptom) == symptom.strip().lower())
    rows = (
        (await db.execute(q.order_by(SymptomEntry.recorded_at.desc()).limit(limit))).scalars().all()
    )
    return EntryListOut(entries=[EntryOut.model_validate(r) for r in rows], total=len(rows))


@router.post("", response_model=EntryOut, status_code=status.HTTP_201_CREATED)
async def create_entry(
    body: EntryCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> EntryOut:
    recorded = _aware(body.recorded_at) if body.recorded_at else datetime.now(UTC)
    if recorded > datetime.now(UTC) + timedelta(minutes=5):
        raise HTTPException(422, "Entry time is in the future.")
    entry = SymptomEntry(
        user_id=user.id,
        symptom=body.symptom,
        severity=body.severity,
        notes=(body.notes or None),
        recorded_at=recorded,
    )
    db.add(entry)
    await db.flush()
    await db.refresh(entry)
    return EntryOut.model_validate(entry)


@router.patch("/{entry_id}", response_model=EntryOut)
async def update_entry(
    entry_id: uuid.UUID, body: EntryUpdate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> EntryOut:
    entry = await _get_owned(db, user, entry_id)
    data = body.model_dump(exclude_unset=True)
    if "symptom" in data and data["symptom"] is not None:
        data["symptom"] = data["symptom"].strip()
    if data.get("recorded_at"):
        data["recorded_at"] = _aware(data["recorded_at"])
    for k, v in data.items():
        if v is not None or k == "notes":
            setattr(entry, k, v)
    await db.flush()
    await db.refresh(entry)
    return EntryOut.model_validate(entry)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    entry_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    entry = await _get_owned(db, user, entry_id)
    await db.delete(entry)
    await db.flush()


def _trend(severities_oldest_first: list[int]) -> str:
    if len(severities_oldest_first) < 2:
        return "steady"
    half = max(1, len(severities_oldest_first) // 2)
    early = sum(severities_oldest_first[:half]) / half
    late_part = severities_oldest_first[-half:]
    late = sum(late_part) / len(late_part)
    if late <= early - 1:
        return "improving"
    if late >= early + 1:
        return "worsening"
    return "steady"


@router.get("/summary", response_model=JournalSummaryOut)
async def summary(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=30, ge=1, le=365),
) -> JournalSummaryOut:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        (
            await db.execute(
                select(SymptomEntry)
                .where(SymptomEntry.user_id == user.id, SymptomEntry.recorded_at >= since)
                .order_by(SymptomEntry.recorded_at.asc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return JournalSummaryOut(
            days=days, total_entries=0, avg_severity=None, symptoms=[], daily=[]
        )

    by_symptom: dict[str, list[SymptomEntry]] = defaultdict(list)
    by_day: dict = defaultdict(list)
    for r in rows:
        by_symptom[r.symptom.lower()].append(r)
        by_day[_aware(r.recorded_at).date()].append(r.severity)

    stats = []
    for items in by_symptom.values():
        sev = [i.severity for i in items]
        stats.append(
            SymptomStat(
                symptom=items[-1].symptom,
                count=len(items),
                avg_severity=round(sum(sev) / len(sev), 1),
                latest_severity=sev[-1],
                trend=_trend(sev),
            )
        )
    stats.sort(key=lambda s: (-s.count, s.symptom))

    daily = [
        DailyPoint(date=d, count=len(v), avg_severity=round(sum(v) / len(v), 1))
        for d, v in sorted(by_day.items())
    ]
    all_sev = [r.severity for r in rows]
    return JournalSummaryOut(
        days=days,
        total_entries=len(rows),
        avg_severity=round(sum(all_sev) / len(all_sev), 1),
        symptoms=stats,
        daily=daily,
    )
