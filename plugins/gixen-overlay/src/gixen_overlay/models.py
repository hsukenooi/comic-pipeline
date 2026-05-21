"""Pydantic models for comic overlay API endpoints."""
from __future__ import annotations

from pydantic import BaseModel, field_validator


class UpsertComicRequest(BaseModel):
    title: str
    issue: str
    year: int | None = None
    grade: float | None = None
    fmv_low: float | None = None
    fmv_high: float | None = None
    fmv_comps: int | None = None
    fmv_confidence: str | None = None
    fmv_notes: str | None = None
    locg_id: int | None = None
    locg_variant_id: int | None = None

    @field_validator("fmv_confidence")
    @classmethod
    def validate_confidence(cls, v: str | None) -> str | None:
        if v is not None and v not in ("high", "medium", "low"):
            raise ValueError("fmv_confidence must be high, medium, or low")
        return v


class LocgLinkRequest(BaseModel):
    locg_id: int
    locg_variant_id: int | None = None
    issue: str | None = None  # if set, target a specific issue within a lot
