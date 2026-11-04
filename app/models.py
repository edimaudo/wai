from datetime import datetime, date, timezone
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

def utcnow():
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="user")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(180), unique=True)
    category: Mapped[str] = mapped_column(String(60), default="funding")
    url: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    frequency_days: Mapped[int] = mapped_column(Integer, default=14)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String(40), default="not_checked")
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_title: Mapped[str] = mapped_column(String(250), default="")
    last_content_hash: Mapped[str] = mapped_column(String(64), default="")
    last_excerpt: Mapped[str] = mapped_column(Text, default="")
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class Opportunity(Base):
    __tablename__ = "opportunities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(240))
    funder: Mapped[str] = mapped_column(String(180))
    summary: Mapped[str] = mapped_column(Text)
    focus_areas: Mapped[str] = mapped_column(Text, default="")
    geography: Mapped[str] = mapped_column(String(180), default="International")
    eligibility: Mapped[str] = mapped_column(Text, default="Requires verification against the call guidance.")
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="monitoring")
    fit_score: Mapped[int] = mapped_column(Integer, default=0)
    fit_rationale: Mapped[str] = mapped_column(Text, default="")
    uncertainties: Mapped[str] = mapped_column(Text, default="")
    case_for_support: Mapped[str] = mapped_column(Text, default="")
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    source_url: Mapped[str] = mapped_column(Text, default="")
    source_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    data_label: Mapped[str] = mapped_column(String(60), default="Verified source lead")
    decision: Mapped[str] = mapped_column(String(40), default="unreviewed")
    decision_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source = relationship("Source")

class Donor(Base):
    __tablename__ = "donors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    synthetic_ref: Mapped[str] = mapped_column(String(40), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    segment: Mapped[str] = mapped_column(String(60), default="Individual")
    giving_summary: Mapped[str] = mapped_column(Text, default="")
    last_gift_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_interaction: Mapped[str] = mapped_column(Text, default="")
    preferences: Mapped[str] = mapped_column(Text, default="Email preferred")
    restrictions: Mapped[str] = mapped_column(Text, default="")
    open_follow_up: Mapped[str] = mapped_column(Text, default="")
    engagement_status: Mapped[str] = mapped_column(String(30), default="review")
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)

class ReportMetric(Base):
    __tablename__ = "report_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[str] = mapped_column(String(20), index=True)
    metric: Mapped[str] = mapped_column(String(100), index=True)
    value: Mapped[float] = mapped_column(Float)
    definition: Mapped[str] = mapped_column(Text, default="")
    source_label: Mapped[str] = mapped_column(String(160), default="Synthetic demonstration dataset")

class PartnerCase(Base):
    __tablename__ = "partner_cases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(180))
    partner_type: Mapped[str] = mapped_column(String(80), default="Corporate")
    overview: Mapped[str] = mapped_column(Text, default="")
    risk_level: Mapped[str] = mapped_column(String(30), default="review")
    status: Mapped[str] = mapped_column(String(40), default="pending_review")
    findings: Mapped[str] = mapped_column(Text, default="")
    uncertainties: Mapped[str] = mapped_column(Text, default="")
    questions: Mapped[str] = mapped_column(Text, default="")
    framework_note: Mapped[str] = mapped_column(Text, default="Demonstration criteria are assumptions pending an approved WaterAid framework.")
    is_fictional: Mapped[bool] = mapped_column(Boolean, default=True)

class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    module: Mapped[str] = mapped_column(String(50), index=True)
    subject_type: Mapped[str] = mapped_column(String(60))
    subject_id: Mapped[str] = mapped_column(String(80))
    claim: Mapped[str] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(String(40), default="observation")
    source_name: Mapped[str] = mapped_column(String(180), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    source_reference: Mapped[str] = mapped_column(String(160), default="")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[str] = mapped_column(String(30), default="medium")
    caveat: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class WorkTask(Base):
    __tablename__ = "work_tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    module: Mapped[str] = mapped_column(String(50), default="general")
    related_type: Mapped[str] = mapped_column(String(60), default="")
    related_id: Mapped[str] = mapped_column(String(80), default="")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    owner: Mapped[str] = mapped_column(String(120), default="Fundraising Lead")
    status: Mapped[str] = mapped_column(String(30), default="open")
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    module: Mapped[str] = mapped_column(String(50))
    subject_type: Mapped[str] = mapped_column(String(60), default="")
    subject_id: Mapped[str] = mapped_column(String(80), default="")
    category: Mapped[str] = mapped_column(String(80), default="usefulness")
    rating: Mapped[int] = mapped_column(Integer, default=3)
    comment: Mapped[str] = mapped_column(Text, default="")
    reviewer: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class GrantProject(Base):
    __tablename__ = "grant_projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    funder: Mapped[str] = mapped_column(String(180))
    amount: Mapped[float] = mapped_column(Float, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="CAD")
    status: Mapped[str] = mapped_column(String(40), default="active")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

class GrantDocument(Base):
    __tablename__ = "grant_documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("grant_projects.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(250))
    stored_path: Mapped[str] = mapped_column(Text)
    document_type: Mapped[str] = mapped_column(String(50), default="supporting")
    version: Mapped[int] = mapped_column(Integer, default=1)
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[str] = mapped_column(String(80))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    project = relationship("GrantProject")

class GrantObligation(Base):
    __tablename__ = "grant_obligations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("grant_projects.id"), nullable=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("grant_documents.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    clause_ref: Mapped[str] = mapped_column(String(100), default="Not mapped")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    frequency: Mapped[str] = mapped_column(String(50), default="once")
    severity: Mapped[str] = mapped_column(String(30), default="medium")
    status: Mapped[str] = mapped_column(String(30), default="open")
    notes: Mapped[str] = mapped_column(Text, default="")

class AppSetting(Base):
    __tablename__ = "app_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    value: Mapped[str] = mapped_column(Text)


class NetworkRelation(Base):
    __tablename__ = "network_relations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_entity: Mapped[str] = mapped_column(String(180), index=True)
    to_entity: Mapped[str] = mapped_column(String(180), index=True)
    relation_type: Mapped[str] = mapped_column(String(100))
    context: Mapped[str] = mapped_column(Text, default="")
    source_name: Mapped[str] = mapped_column(String(180), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[str] = mapped_column(String(40), default="unverified")
    status: Mapped[str] = mapped_column(String(40), default="to_verify")
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
