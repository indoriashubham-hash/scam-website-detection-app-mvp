"""SQLAlchemy models. Mirror of db/001_initial.sql. Keep in sync."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Investigation(Base):
    __tablename__ = "investigations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    input_url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_origin: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="queued", nullable=False)
    risk_band: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    # Verdict written by the Risk Aggregator after the crawl completes. `summary`
    # is a short, human-readable narrative; `findings` is the machine-readable
    # top-N signals that drove the band, stable enough for a UI/report writer
    # to render without re-walking the evidence table.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    findings: Mapped[list[dict]] = mapped_column(JSONB, default=list, nullable=False)
    # LLM-produced narrative object (see app/reporter/narrative.py). NULL if
    # no API key, or the LLM output was rejected by validation.
    narrative: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # LLM-produced deep review (see app/reporter/deep.py). Populated
    # on-demand by the /deep-review endpoint; cached here.
    deep_review: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pages: Mapped[list[Page]] = relationship(back_populates="investigation", cascade="all, delete-orphan")
    evidence: Mapped[list[Evidence]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan"
    )


class Page(Base):
    __tablename__ = "pages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    render_mode: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    screenshot_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    ato_screenshot_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    html_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    har_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    investigation: Mapped[Investigation] = relationship(back_populates="pages")
    forms: Mapped[list[Form]] = relationship(back_populates="page", cascade="all, delete-orphan")
    outlinks: Mapped[list[Outlink]] = relationship(back_populates="page", cascade="all, delete-orphan")


class Form(Base):
    __tablename__ = "forms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pages.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    method: Mapped[str | None] = mapped_column(String(8), nullable=True)
    fields: Mapped[list[dict]] = mapped_column(JSONB, default=list, nullable=False)
    is_login: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_payment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    posts_cross_origin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    page: Mapped[Page] = relationship(back_populates="forms")


class Outlink(Base):
    __tablename__ = "outlinks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pages.id", ondelete="CASCADE"), nullable=False
    )
    href: Mapped[str] = mapped_column(Text, nullable=False)
    rel: Mapped[str | None] = mapped_column(Text, nullable=True)
    anchor_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    same_origin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    registered_domain: Mapped[str | None] = mapped_column(Text, nullable=True)

    page: Mapped[Page] = relationship(back_populates="outlinks")


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False
    )
    analyzer: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0.5, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    screenshot_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    investigation: Mapped[Investigation] = relationship(back_populates="evidence")
