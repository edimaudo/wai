from __future__ import annotations

import csv

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AppSetting,
    Donor,
    Evidence,
    GrantDocument,
    GrantObligation,
    GrantProject,
    NetworkRelation,
    Opportunity,
    PartnerCase,
    ReportMetric,
    Source,
    User,
    WorkTask,
)
from app.security import verify_password

SOURCES = [
    {
        "name": "Global Affairs Canada — International Assistance Calls",
        "category": "Canadian government funder",
        "url": "https://www.international.gc.ca/world-monde/funding-financement/open_calls-appels_ouverts.aspx?lang=eng",
        "description": "Official list of current and upcoming Global Affairs Canada international assistance opportunities.",
        "frequency_days": 7,
    },
    {
        "name": "Global Affairs Canada — Unsolicited Concept Notes",
        "category": "Canadian government funder",
        "url": "https://www.international.gc.ca/world-monde/funding-financement/apply_funding-demande_financement.aspx?lang=eng",
        "description": "Official process guidance for unsolicited international assistance concept notes.",
        "frequency_days": 30,
    },
    {
        "name": "IDRC — Funding",
        "category": "Canadian research funder",
        "url": "https://idrc-crdi.ca/en/funding",
        "description": "Official IDRC page listing open and closed research funding calls.",
        "frequency_days": 14,
    },
    {
        "name": "Grand Challenges Canada — Apply for Funding",
        "category": "Canadian innovation funder",
        "url": "https://www.grandchallenges.ca/apply-for-funding/",
        "description": "Official current and past Grand Challenges Canada funding opportunities.",
        "frequency_days": 14,
    },
]

# These are editable operational defaults, not generated business records.
DEFAULT_SETTINGS = {
    "criteria_wash": "30",
    "criteria_geography": "20",
    "criteria_eligibility": "20",
    "criteria_timing": "15",
    "criteria_capacity": "15",
    "criteria_focus_terms": "water, sanitation, hygiene, WASH, climate-resilient WASH, eau, assainissement, hygiène, EAH, résilience climatique",
    "criteria_geography_terms": "international, ODA-eligible, Sub-Saharan Africa, Africa, Asia, Pacific, admissible à l’APD, Afrique subsaharienne, Afrique, Asie, Pacifique",
    "criteria_ineligibility_terms": "invitation only, invitation-only, individual fellowship, individual applicant, sur invitation seulement, bourse individuelle, demandeur individuel",
    "criteria_eligibility_rules": "Canadian-registered nonprofit or eligible international development partner;\nInternational WASH programme fit;\nConfirm country and applicant eligibility against each official call.",
    "default_frequency_days": "14",
}


def initialize_operational_defaults(db: Session) -> None:
    """Initialize real source definitions and editable settings only."""
    if db.query(Source).count() == 0:
        db.add_all(Source(**item, enabled=True, last_status="not_checked") for item in SOURCES)

    existing_settings = {row.key for row in db.query(AppSetting).all()}
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing_settings:
            db.add(AppSetting(key=key, value=value))
    db.commit()


def purge_demo_records(db: Session) -> dict[str, int]:
    """Delete identifiable fabricated records created by earlier releases.

    The cleanup uses explicit legacy flags, exact seed labels and exact seed
    identifiers. It does not delete source CSV files or staff-entered records.
    It is idempotent and safe to run at application startup.
    """
    removed: dict[str, int] = {}

    def delete_evidence(subject_type: str, subject_ids: set[str]) -> None:
        if subject_ids:
            db.query(Evidence).filter(
                Evidence.subject_type == subject_type,
                Evidence.subject_id.in_(subject_ids),
            ).delete(synchronize_session=False)

    # Delete only the original default accounts when their original passwords
    # are unchanged. A real administrator account with a changed password is kept.
    for username, password in (
        ("admin", "WaiAdmin!2026"),
        ("fundraiser", "WaiUser!2026"),
    ):
        account = db.query(User).filter(User.username == username).first()
        if account and verify_password(password, account.password_hash):
            db.delete(account)
            removed["legacy_accounts"] = removed.get("legacy_accounts", 0) + 1

    opportunity_labels = [
        "Official pathway — staff verification required",
        "Official open call — eligibility gate unresolved",
        "Official opportunity — fit uncertain",
        "Official listing — applicant type unresolved",
    ]
    demo_opportunities = db.query(Opportunity).filter(
        Opportunity.data_label.in_(opportunity_labels)
    ).all()
    opportunity_ids = {str(item.id) for item in demo_opportunities}
    delete_evidence("opportunity", opportunity_ids)
    for item in demo_opportunities:
        db.delete(item)
    removed["opportunities"] = len(demo_opportunities)

    demo_donors = db.query(Donor).filter(or_(
        Donor.synthetic.is_(True),
        Donor.synthetic_ref.like("SYN-DON-%"),
    )).all()
    donor_refs = {item.synthetic_ref for item in demo_donors}
    delete_evidence("donor", donor_refs)
    delete_evidence("constituent", donor_refs)
    if donor_refs:
        db.query(WorkTask).filter(
            WorkTask.related_type == "donor",
            WorkTask.related_id.in_(donor_refs),
        ).delete(synchronize_session=False)
    for item in demo_donors:
        db.delete(item)
    removed["donors"] = len(demo_donors)

    demo_cases = db.query(PartnerCase).filter(or_(
        PartnerCase.is_fictional.is_(True),
        PartnerCase.name.ilike("%(fictional)%"),
    )).all()
    case_ids = {str(item.id) for item in demo_cases}
    delete_evidence("partner_case", case_ids)
    for item in demo_cases:
        db.delete(item)
    removed["partner_cases"] = len(demo_cases)

    demo_network = db.query(NetworkRelation).filter(or_(
        NetworkRelation.synthetic.is_(True),
        NetworkRelation.source_name == "Synthetic relationship scenario",
        NetworkRelation.from_entity.ilike("%(synthetic)%"),
        NetworkRelation.to_entity.ilike("%(fictional)%"),
    )).all()
    network_ids = {str(item.id) for item in demo_network}
    delete_evidence("network_relation", network_ids)
    for item in demo_network:
        db.delete(item)
    removed["network_relations"] = len(demo_network)

    demo_projects = db.query(GrantProject).filter(or_(
        GrantProject.status.ilike("%synthetic%"),
        GrantProject.name.ilike("%synthetic case%"),
        GrantProject.funder.ilike("%(fictional)%"),
    )).all()
    project_ids = {item.id for item in demo_projects}
    obligation_conditions = [
        GrantObligation.notes.ilike("%synthetic obligation%"),
        GrantObligation.clause_ref.ilike("%illustrative%"),
    ]
    if project_ids:
        obligation_conditions.append(GrantObligation.project_id.in_(project_ids))
    demo_obligations = db.query(GrantObligation).filter(or_(*obligation_conditions)).all()
    obligation_ids = {str(item.id) for item in demo_obligations}
    delete_evidence("obligation", obligation_ids)
    for item in demo_obligations:
        db.delete(item)

    demo_documents = []
    if project_ids:
        demo_documents = db.query(GrantDocument).filter(
            GrantDocument.project_id.in_(project_ids)
        ).all()
    document_ids = {str(item.id) for item in demo_documents}
    delete_evidence("grant_document", document_ids)
    for item in demo_documents:
        db.delete(item)
    project_refs = {str(item.id) for item in demo_projects}
    delete_evidence("grant_project", project_refs)
    for item in demo_projects:
        db.delete(item)
    removed["grant_projects"] = len(demo_projects)
    removed["grant_obligations"] = len(demo_obligations)
    removed["grant_document_records"] = len(demo_documents)

    demo_metrics = db.query(ReportMetric).filter(
        ReportMetric.source_label == "Synthetic demonstration dataset"
    ).all()
    for item in demo_metrics:
        db.delete(item)
    removed["report_metrics"] = len(demo_metrics)

    demo_evidence = db.query(Evidence).filter(or_(
        Evidence.classification == "synthetic donor record",
        Evidence.source_name == "Synthetic donor dataset",
    )).all()
    for item in demo_evidence:
        db.delete(item)
    removed["evidence"] = len(demo_evidence)

    db.commit()
    return removed


def seed_app(db: Session) -> None:
    """Compatibility entry point used by the current app startup lifecycle.

    It initializes operational configuration and purges legacy fabricated data.
    It never creates demo credentials, sample donors, reports, partnerships,
    grants, obligations, or network connections.
    """
    initialize_operational_defaults(db)
    purge_demo_records(db)


SOURCE_CSV_FILES = (
    "gift_transactions_table.csv",
    "CRM_interacions_table.csv",
    "video_email_data_table.csv",
    "constituent_profile_table.csv",
)


def read_source_records(filename: str) -> tuple[list[str], list[dict[str, str]]]:
    """Read one approved source CSV in place without importing or modifying it."""
    if filename not in SOURCE_CSV_FILES:
        raise ValueError("Source file is not on the approved allow-list.")
    path = settings.wai_data_dir / filename
    if not path.is_file():
        raise FileNotFoundError(filename)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        records = []
        for raw in reader:
            row = {str(key): (value if value is not None else "") for key, value in raw.items()}
            for id_field in ("CONSTITUENT_ID", "CONTACT_ID"):
                if id_field in row:
                    row[id_field] = str(row[id_field])
            records.append(row)
    return columns, records


def load_source_csvs(_db: Session | None = None) -> dict:
    """Return read-only source metadata; never create, import or copy files."""
    folder = settings.wai_data_dir
    result = {"folder": str(folder), "files": [], "rows": 0, "warnings": [], "read_only": True}
    for name in SOURCE_CSV_FILES:
        try:
            columns, records = read_source_records(name)
            result["rows"] += len(records)
            result["files"].append({"name": name, "rows": len(records), "columns": columns})
        except FileNotFoundError:
            result["warnings"].append(f"Missing source file: {name}")
        except Exception as exc:
            result["warnings"].append(f"Could not read {name}: {exc.__class__.__name__}")
    return result
