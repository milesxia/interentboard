from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TopicCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=2000)
    description: str = Field(default="", max_length=4000)
    enabled: bool = True
    priority: int = Field(default=50, ge=0, le=100)


class TopicUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    query: str | None = Field(default=None, min_length=1, max_length=2000)
    description: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=100)


class TopicOut(ORMModel):
    id: int
    name: str
    query: str
    description: str
    enabled: bool
    priority: int


class RunOut(ORMModel):
    id: int
    topic_id: int
    status: str
    progress: int
    message: str
    search_round: int
    retry_count: int
    error: str
    summary: str
    trend: str
    prediction: str
    confidence: float


class ManualNoteCreate(BaseModel):
    topic_id: int
    title: str = Field(default="", max_length=300)
    content: str = Field(min_length=1, max_length=50000)


class ManualClaimCreate(BaseModel):
    topic_id: int
    claim_text: str = Field(min_length=1, max_length=10000)
    category: str = Field(default="manual", max_length=120)
    event_time: str = Field(default="", max_length=100)
    importance: int = Field(default=5, ge=0, le=10)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ManualClaimUpdate(BaseModel):
    claim_text: str | None = Field(default=None, min_length=1, max_length=10000)
    category: str | None = Field(default=None, max_length=120)
    event_time: str | None = Field(default=None, max_length=100)
    importance: int | None = Field(default=None, ge=0, le=10)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: Literal["active", "superseded", "rejected"] | None = None


class ClaimOut(ORMModel):
    id: int
    topic_id: int
    run_id: int | None
    claim_text: str
    category: str
    claim_type: str
    event_time: str
    confidence: float
    importance: int
    trend: str
    prediction: str
    priority: int
    origin: str
    status: str
    occurrence_count: int


class SourceOut(ORMModel):
    id: int
    topic_id: int
    run_id: int | None
    url: str
    title: str
    source_time: datetime | None
    retrieved_at: datetime
    mime_type: str
    storage_path: str
    seen_count: int


class ConflictOut(ORMModel):
    id: int
    topic_id: int
    run_id: int | None
    claim_a_id: int | None
    claim_b_id: int | None
    claim_a_text: str
    claim_b_text: str
    reason: str
    confidence: float
    status: str
    resolution: str


class ConflictResolve(BaseModel):
    resolution: str = Field(min_length=1, max_length=10000)
    winning_claim_id: int | None = None


class WebsiteWatchCreate(BaseModel):
    topic_id: int
    url: str = Field(min_length=8, max_length=4000)
    enabled: bool = True


class WebsiteWatchOut(ORMModel):
    id: int
    topic_id: int
    url: str
    enabled: bool
    last_hash: str
    last_checked_at: datetime | None
    last_changed_at: datetime | None


class ExtractedEntity(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    type: Literal["company", "product", "person", "technology", "organization", "place", "other"] = "other"
    description: str = Field(default="", max_length=1000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ExtractedClaim(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    category: str = Field(default="general", max_length=120)
    type: Literal["fact", "inference"] = "fact"
    event_time: str = Field(default="", max_length=100)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    importance: int = Field(default=5, ge=0, le=10)
    entity_names: list[str] = Field(default_factory=list, max_length=20)


class ExtractedRelation(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    predicate: str = Field(min_length=1, max_length=200)
    object: str = Field(min_length=1, max_length=300)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ChunkAnalysis(BaseModel):
    title: str = Field(default="", max_length=300)
    category: str = Field(default="general", max_length=120)
    entities: list[ExtractedEntity] = Field(default_factory=list, max_length=30)
    claims: list[ExtractedClaim] = Field(default_factory=list, max_length=30)
    relations: list[ExtractedRelation] = Field(default_factory=list, max_length=30)
    search_gaps: list[str] = Field(default_factory=list, max_length=8)
    importance: int = Field(default=5, ge=0, le=10)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class SynthesisConflict(BaseModel):
    claim_a: str = Field(min_length=1, max_length=3000)
    claim_b: str = Field(min_length=1, max_length=3000)
    reason: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class RunSynthesis(BaseModel):
    title: str = Field(default="", max_length=300)
    category: str = Field(default="general", max_length=120)
    summary: str = Field(default="", max_length=8000)
    trend: str = Field(default="", max_length=4000)
    prediction: str = Field(default="", max_length=4000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    importance: int = Field(default=5, ge=0, le=10)
    conflicts: list[SynthesisConflict] = Field(default_factory=list, max_length=30)
    followup_queries: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("followup_queries")
    @classmethod
    def dedupe_queries(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in value:
            normalized = " ".join(item.split())[:500]
            if normalized and normalized.lower() not in seen:
                seen.add(normalized.lower())
                out.append(normalized)
        return out[:5]
