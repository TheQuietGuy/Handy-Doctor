"""app/journal/schemas.py - request/response models for the symptom journal."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EntryCreate(BaseModel):
    symptom: str = Field(min_length=1, max_length=120)
    severity: int = Field(ge=1, le=10)
    notes: str | None = Field(default=None, max_length=2000)
    recorded_at: datetime | None = None  # defaults to now

    @field_validator("symptom")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Symptom cannot be blank.")
        return v


class EntryUpdate(BaseModel):
    symptom: str | None = Field(default=None, min_length=1, max_length=120)
    severity: int | None = Field(default=None, ge=1, le=10)
    notes: str | None = Field(default=None, max_length=2000)
    recorded_at: datetime | None = None


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    symptom: str
    severity: int
    notes: str | None
    recorded_at: datetime
    created_at: datetime


class EntryListOut(BaseModel):
    entries: list[EntryOut]
    total: int


class SymptomStat(BaseModel):
    symptom: str
    count: int
    avg_severity: float
    latest_severity: int
    trend: str  # "improving" | "worsening" | "steady"


class DailyPoint(BaseModel):
    date: date
    count: int
    avg_severity: float


class JournalSummaryOut(BaseModel):
    days: int
    total_entries: int
    avg_severity: float | None
    symptoms: list[SymptomStat]
    daily: list[DailyPoint]
