from datetime import date, datetime, timezone
from pathlib import Path
import csv
from sqlalchemy.orm import Session
from app.config import settings
from app.models import (User, Source, Opportunity, Donor, ReportMetric, PartnerCase, Evidence,
                        WorkTask, GrantProject, GrantObligation, AppSetting, NetworkRelation)
from app.security import hash_password

SOURCES = [
    {
      "name":"Global Affairs Canada — International Assistance Calls",
      "category":"Canadian government funder",
      "url":"https://www.international.gc.ca/world-monde/funding-financement/open_calls-appels_ouverts.aspx?lang=eng",
      "description":"Official list of current and upcoming Global Affairs Canada international assistance opportunities.",
      "frequency_days":7
    },
    {
      "name":"Global Affairs Canada — Unsolicited Concept Notes",
      "category":"Canadian government funder",
      "url":"https://www.international.gc.ca/world-monde/funding-financement/apply_funding-demande_financement.aspx?lang=eng",
      "description":"Official process guidance for unsolicited international assistance concept notes.",
      "frequency_days":30
    },
    {
      "name":"IDRC — Funding",
      "category":"Canadian research funder",
      "url":"https://idrc-crdi.ca/en/funding",
      "description":"Official IDRC page listing open and closed research funding calls.",
      "frequency_days":14
    },
    {
      "name":"Grand Challenges Canada — Apply for Funding",
      "category":"Canadian innovation funder",
      "url":"https://www.grandchallenges.ca/apply-for-funding/",
      "description":"Official current and past Grand Challenges Canada funding opportunities.",
      "frequency_days":14
    },
]


def seed_users(db: Session):
    if not settings.demo_seed_users:
        return
    if not db.query(User).filter_by(username=settings.demo_admin_username).first():
        db.add(User(username=settings.demo_admin_username, display_name=settings.admin_display_name,
                    role="admin", password_hash=hash_password(settings.demo_admin_password), is_active=True))
    if not db.query(User).filter_by(username=settings.demo_user_username).first():
        db.add(User(username=settings.demo_user_username, display_name=settings.user_display_name,
                    role="user", password_hash=hash_password(settings.demo_user_password), is_active=True))
    db.commit()


def seed_app(db: Session):
    seed_users(db)
    if db.query(Source).count() == 0:
        for item in SOURCES:
            db.add(Source(**item, enabled=True, last_status="not_checked"))
        db.commit()

    # Application defaults are operational configuration, not demo records.
    defaults = {
      "criteria_wash": "30", "criteria_geography": "20", "criteria_eligibility": "20", "criteria_timing": "15", "criteria_capacity": "15",
      "criteria_focus_terms": "water, sanitation, hygiene, WASH, climate-resilient WASH, eau, assainissement, hygiène, EAH, résilience climatique",
      "criteria_geography_terms": "international, ODA-eligible, Sub-Saharan Africa, Africa, Asia, Pacific, international, admissible à l’APD, Afrique subsaharienne, Afrique, Asie, Pacifique",
      "criteria_ineligibility_terms": "invitation only, invitation-only, individual fellowship, individual applicant, sur invitation seulement, bourse individuelle, demandeur individuel",
      "criteria_eligibility_rules": "Canadian-registered nonprofit or eligible international development partner;\nInternational WASH programme fit;\nConfirm country and applicant eligibility against each official call.",
      "default_frequency_days":"14", "review_cost_minutes":"20", "source_scan_monthly_cost":"35", "opportunity_review_minutes":"20"
    }
    for key, value in defaults.items():
        if not db.query(AppSetting).filter_by(key=key).first():
            db.add(AppSetting(key=key, value=value))
    db.commit()

    # DEMO_SEED_DATA=false leaves production tables empty for staff-owned records.
    # Source definitions and default fit criteria above are still initialized.
    if not settings.demo_seed_data:
        return

    if db.query(Opportunity).count() == 0:
        src = {s.name: s for s in db.query(Source).all()}
        gac_calls = src["Global Affairs Canada — International Assistance Calls"]
        gac_concept = src["Global Affairs Canada — Unsolicited Concept Notes"]
        idrc = src["IDRC — Funding"]
        gcc = src["Grand Challenges Canada — Apply for Funding"]
        now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        records = [
          Opportunity(
            title="Unsolicited concept note — international development",
            funder="Global Affairs Canada", summary="A two-step pathway for organizations to submit an unsolicited concept note for an international development project. An invitation to submit a full proposal follows only if the concept note is of interest.",
            focus_areas="International development; poverty reduction; Feminist International Assistance Policy priorities",
            geography="ODA-eligible countries", eligibility="The official guidance describes non-governmental and private-sector organizations as potential applicants. Confirm WaterAid Canada's legal status, registration and program-specific conditions before proceeding.",
            deadline=None, status="ongoing pathway", fit_score=78,
            fit_rationale="Potentially relevant to WaterAid Canada's international WASH programming and long-term systems strengthening. This is an unsolicited pathway rather than an open competitive call; fit depends on alignment with current Global Affairs Canada priorities and programme capacity.",
            uncertainties="No published closing deadline. The process is selective and does not guarantee funding. Current policy and programme priorities, eligible costs and organization-specific eligibility require staff verification.",
            case_for_support="Explore a concept note that positions locally led, climate-resilient WASH systems as a contribution to sustainable development outcomes. Ground any case in approved programme evidence and current GAC priorities.",
            source_id=gac_concept.id, source_url=gac_concept.url, source_checked_at=now, data_label="Official pathway — staff verification required", decision="unreviewed"),
          Opportunity(
            title="Canadian Climate Partnerships Initiative",
            funder="Global Affairs Canada", summary="An invitation-only funding process supporting secure, low-carbon and climate-resilient economies in Sub-Saharan Africa, with adaptation and mitigation benefits for vulnerable communities.",
            focus_areas="Climate adaptation; climate mitigation; resilience; Sub-Saharan Africa", geography="Sub-Saharan Africa",
            eligibility="Invitation only. The published page states that only organizations previously funded through Partnering for Climate are eligible. WaterAid Canada's status against that condition is not known in this demonstration.",
            deadline=date(2026,11,17), status="open — invitation only", fit_score=42,
            fit_rationale="Climate resilience and community systems work may intersect with climate-resilient WASH, but the invitation-only eligibility condition is a significant barrier.",
            uncertainties="Confirm whether WaterAid Canada received Partnering for Climate funding and whether it was invited. The tool must not infer either condition.",
            case_for_support="Only develop a case if the organization confirms invitation and eligibility. Potential alignment would need to show a clear, evidence-backed WASH contribution to adaptation and resilience outcomes.",
            source_id=gac_calls.id, source_url="https://www.international.gc.ca/world-monde/funding-financement/2026-09-15-climate_partnerships-partenariats_climat.aspx?lang=eng",
            source_checked_at=now, data_label="Official open call — eligibility gate unresolved", decision="needs verification"),
          Opportunity(
            title="Nexa Lighthouse Funding Opportunity: Asia and the Pacific",
            funder="Grand Challenges Canada", summary="Funding to scale proven innovations that help local health actors respond to climate-related health risks, including mosquito-borne infections, extreme heat and poor air quality.",
            focus_areas="Climate and health; health service delivery; Asia and the Pacific", geography="Asia and the Pacific",
            eligibility="Consult the linked full opportunity and applicant guidance. Eligibility and suitability for a WASH organization are not established by the overview page.",
            deadline=date(2026,11,17), status="open — full guidance required", fit_score=36,
            fit_rationale="Potential indirect intersection through climate, health and community resilience, but the call is framed around climate-related health service delivery rather than WASH systems.",
            uncertainties="Review the full call, eligible applicant types, geographic restrictions, partnership requirements, funding size and whether WASH interventions are in scope.",
            case_for_support="Do not prioritize unless the full guidance confirms WASH-related interventions and an appropriate health-sector partnership can be demonstrated.",
            source_id=gcc.id, source_url=gcc.url, source_checked_at=now, data_label="Official opportunity — fit uncertain", decision="unreviewed"),
          Opportunity(
            title="John G. Bene Fellowship 2026",
            funder="International Development Research Centre", summary="A fellowship opportunity listed by IDRC with a November 30, 2026 deadline.",
            focus_areas="Research fellowship; international development", geography="See call guidance",
            eligibility="The funding page lists this as a fellowship. The eligible applicant profile must be checked; it may be intended for an individual rather than an organizational grant applicant.",
            deadline=date(2026,11,30), status="open — applicant type to verify", fit_score=18,
            fit_rationale="Low organizational fit based on the fellowship format; it may not be a direct funding route for WaterAid Canada.",
            uncertainties="Applicant eligibility, topic scope and whether an organization can nominate or host an applicant are not established in the summary listing.",
            case_for_support="Do not invest in a full application assessment until the fellowship guidance confirms a valid route for WaterAid staff or partners.",
            source_id=idrc.id, source_url=idrc.url, source_checked_at=now, data_label="Official listing — applicant type unresolved", decision="decline pending verification"),
        ]
        db.add_all(records)
        db.commit()
        for item in db.query(Opportunity).all():
            db.add(Evidence(module="prospecting", subject_type="opportunity", subject_id=str(item.id),
                            claim=f"{item.title}: {item.eligibility}", classification="source-backed summary",
                            source_name=item.funder, source_url=item.source_url,
                            source_reference="Funding page / opportunity guidance", checked_at=now,
                            confidence="medium", caveat="Confirm current call details and applicant eligibility before a decision."))
        db.commit()
    if db.query(Donor).count() == 0:
        db.add_all([
          Donor(synthetic_ref="SYN-DON-001", display_name="Alex Morgan", segment="Major donor",
                giving_summary="Three gifts over 30 months; most recent gift CAD 2,500. Aggregate summary only.", last_gift_date=date(2026,8,12),
                last_interaction="Thank-you email sent 15 August 2026; donor asked for an update on rural water services.",
                preferences="Email preferred; concise programme updates.", restrictions="Do not mention employer or speculate about personal circumstances.",
                open_follow_up="Send the approved rural water services update; confirm programme facts before use.", engagement_status="follow_up"),
          Donor(synthetic_ref="SYN-DON-002", display_name="Priya Shah", segment="Recurring donor",
                giving_summary="Monthly gifts recorded for 18 months; most recent monthly gift CAD 75.", last_gift_date=date(2026,10,1),
                last_interaction="Survey response received 2 September 2026; requested fewer campaign emails.",
                preferences="Email only for essential stewardship updates; no fundraising calls.", restrictions="Do not add to campaign outreach; no telephone contact.",
                open_follow_up="Review contact frequency and confirm the recorded communication preferences are current.", engagement_status="protect_preference"),
          Donor(synthetic_ref="SYN-DON-003", display_name="Taylor Brooks", segment="Individual donor",
                giving_summary="Two gifts recorded over 12 months; most recent gift CAD 250.", last_gift_date=date(2026,6,18),
                last_interaction="A note with the June gift expressed interest in community-led sanitation.",
                preferences="Postal and email updates are acceptable.", restrictions="No current restriction recorded. Do not infer a capacity to give more.",
                open_follow_up="Share a factual community-led sanitation update and offer an optional conversation.", engagement_status="review")
        ])
        db.commit()
    if db.query(ReportMetric).count() == 0:
        metric_rows = [
          ("2025-Q1","fundraising_revenue",182000,"CAD received during the quarter."),
          ("2025-Q2","fundraising_revenue",198000,"CAD received during the quarter."),
          ("2025-Q3","fundraising_revenue",221000,"CAD received during the quarter."),
          ("2025-Q4","fundraising_revenue",309000,"CAD received during the quarter; synthetic scenario includes a one-time matching gift."),
          ("2026-Q1","fundraising_revenue",204000,"CAD received during the quarter."),
          ("2026-Q2","fundraising_revenue",228000,"CAD received during the quarter."),
          ("2026-Q3","fundraising_revenue",247000,"CAD received during the quarter."),
          ("2025-Q1","active_donors",820,"Distinct donors with at least one gift in the quarter."),
          ("2025-Q2","active_donors",838,"Distinct donors with at least one gift in the quarter."),
          ("2025-Q3","active_donors",864,"Distinct donors with at least one gift in the quarter."),
          ("2025-Q4","active_donors",932,"Distinct donors with at least one gift in the quarter."),
          ("2026-Q1","active_donors",858,"Distinct donors with at least one gift in the quarter."),
          ("2026-Q2","active_donors",881,"Distinct donors with at least one gift in the quarter."),
          ("2026-Q3","active_donors",899,"Distinct donors with at least one gift in the quarter."),
          ("2025-Q1","email_open_rate",44.0,"Percentage of delivered fundraising emails opened."),
          ("2025-Q2","email_open_rate",42.0,"Percentage of delivered fundraising emails opened."),
          ("2025-Q3","email_open_rate",43.0,"Percentage of delivered fundraising emails opened."),
          ("2025-Q4","email_open_rate",48.0,"Percentage of delivered fundraising emails opened."),
          ("2026-Q1","email_open_rate",40.0,"Percentage of delivered fundraising emails opened."),
          ("2026-Q2","email_open_rate",39.0,"Percentage of delivered fundraising emails opened."),
          ("2026-Q3","email_open_rate",41.0,"Percentage of delivered fundraising emails opened."),
        ]
        db.add_all([ReportMetric(period=p, metric=m, value=v, definition=d) for p,m,v,d in metric_rows])
        db.commit()
    if db.query(PartnerCase).count() == 0:
        db.add_all([
          PartnerCase(name="BlueCurrent Technologies (fictional)", partner_type="Technology company",
            overview="Fictional supplier proposing an in-kind water monitoring partnership. Demonstration file contains an incomplete ownership record and one unverified media reference.",
            risk_level="review", status="pending_review",
            findings="Observed: ownership information in the supplied fictional case file is incomplete. Unverified reference: a media snippet alleges a local environmental dispute but contains no primary documentation.",
            uncertainties="No corporate registry extract, regulator decision, company response or independently corroborating source supplied.",
            questions="Obtain current ownership records; identify relevant subsidiaries; request the partner's response; check environmental and safeguarding controls.",
            framework_note="Assumption for demo: ownership transparency, environmental/social performance, safeguarding and mission alignment are review categories. These are not represented as verbatim WaterAid policy."),
          PartnerCase(name="ClearSpring Community Foundation (fictional)", partner_type="Foundation",
            overview="Fictional foundation proposing a restricted grant for community-led WASH capacity building. Case file contains basic public profile and no adverse information.",
            risk_level="low", status="pending_review",
            findings="Supplied fictional profile describes a community development mandate and transparent board list. No adverse findings are included in the test pack.",
            uncertainties="No finding in the supplied pack does not establish that the foundation is risk-free. Independent screening, funding source checks and conflict review remain outstanding.",
            questions="Verify legal registration, source of funds, board conflicts, geographic operations and any conditions attached to the grant.",
            framework_note="Assumption for demo: ownership/governance, source of funds, mission alignment and operating context are review categories. Not a WaterAid policy determination.")
        ])
        db.commit()
    if db.query(GrantProject).count() == 0:
        project = GrantProject(name="Community-led climate-resilient WASH programme (synthetic case)",
                               funder="Northern Development Partnership (fictional)", amount=250000,
                               status="active — synthetic", start_date=date(2026,4,1), end_date=date(2027,3,31))
        db.add(project); db.commit(); db.refresh(project)
        db.add_all([
          GrantObligation(project_id=project.id, title="Submit quarterly narrative and indicator report", clause_ref="Illustrative clause 4.2", due_date=date(2026,12,15), frequency="quarterly", severity="high", notes="Synthetic obligation. Confirm clause and date against the executed agreement."),
          GrantObligation(project_id=project.id, title="Reconcile expenditure against approved budget", clause_ref="Illustrative schedule B", due_date=date(2026,11,30), frequency="monthly", severity="high", notes="Synthetic obligation. Confirm the applicable reporting period and budget version."),
          GrantObligation(project_id=project.id, title="Complete safeguarding and partner capacity check", clause_ref="Illustrative annex C", due_date=date(2026,12,5), frequency="once", severity="medium", notes="Synthetic obligation. Responsible staff and evidence not yet assigned.")
        ])
        db.commit()
    if db.query(NetworkRelation).count() == 0:
        db.add_all([
          NetworkRelation(from_entity="Board contact A (synthetic)", to_entity="Canadian WASH Partnership Forum (fictional)",
                          relation_type="Sector network membership", context="Synthetic demonstration relationship. A staff member has recorded a possible introduction route; confirm the contact and permission before outreach.",
                          source_name="Synthetic relationship scenario", confidence="unverified", status="to_verify", synthetic=True),
          NetworkRelation(from_entity="Canadian WASH Partnership Forum (fictional)", to_entity="Global Affairs Canada",
                          relation_type="Public-sector engagement pathway", context="Illustrative sector engagement pathway only; this is not evidence of a personal connection, access or funding influence.",
                          source_name="Synthetic relationship scenario", confidence="assumption", status="to_verify", synthetic=True),
          NetworkRelation(from_entity="Programme advisor B (synthetic)", to_entity="Grand Challenges Canada",
                          relation_type="Past event attendance", context="Synthetic example. Confirm attendance, recency, relationship strength and permission before suggesting an introduction.",
                          source_name="Synthetic relationship scenario", confidence="unverified", status="to_verify", synthetic=True),
        ])
        db.commit()
    from app.services.scoring import score_opportunity
    criteria = {row.key:row.value for row in db.query(AppSetting).all()}
    for opportunity in db.query(Opportunity).all():
        opportunity.fit_score, _ = score_opportunity(opportunity, criteria)
    db.commit()


SOURCE_CSV_FILES = (
    "gift_transactions_table.csv",
    "CRM_interacions_table.csv",
    "video_email_data_table.csv",
    "constituent_profile_table.csv",
)


def read_source_records(filename: str) -> tuple[list[str], list[dict[str, str]]]:
    """Read one approved source CSV in place; never copy records into the app database.

    Constituent/contact identifiers remain strings. The source file is opened read-only
    and is never rewritten, renamed, normalized in place, or automatically ingested.
    """
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
    """Return source-folder metadata after read-only inspection; no database writes."""
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
