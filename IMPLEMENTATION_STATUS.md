# Wai Intelligence — implementation status

## Implemented in this starter milestone

- FastAPI application with server-rendered Jinja2 pages and standard web routes.
- Public landing page, sign-in page, authenticated dashboard, module entry pages, administrator pages, and error pages.
- Shared `base.html` template for all page views.
- Supabase Auth session handling, role checks, active-account checks, CSRF protection, and server-side admin operations.
- Admin and Staff roles, with a seed script for demonstration accounts and admin controls to create staff accounts and grant/revoke access.
- Initial unified Supabase schema for all five capability areas, shared evidence, relationships, review records, and audit events.
- RLS policies for active staff reads, self-profile/language access, admin-only audit visibility, and server-side privileged writes.
- Private `data/` folder discovery and CSV ingestion for gifts, CRM interactions, and video/email events.
- CSV validation for required fields, dates, numeric precision, booleans, missing-value markers, row limits, record hashes, duplicate imports, and import history.
- Gemini 3.6 Flash service boundary using `google-genai` and the Gemini Developer API.
- English and Canadian French interface translations.
- Responsive CSS foundation, Inter typography, WaterAid blue/black palette, light/dark modes, and Small/Medium/Large font scaling.
- Docker configuration for Vultr hosting, read-only data mounting, private loopback binding, and an Nginx HTTPS reverse-proxy example.
- Unit tests for CSV normalization and basic validation.

## Not yet implemented

The five module pages and database structures are foundations, not completed agent workflows. The next implementation work is:

1. **Reporting intelligence:** RFM calculation, donation breakdowns, video metrics, Plotly charts, 12-month forecast, evidence-linked narrative drafts, and metric definitions.
2. **Funding intelligence:** curated Canadian funding source catalogue, scheduled source checks, public-announcement detection, eligibility extraction, configurable fit criteria, ranking, and briefing notes.
3. **Donor intelligence:** donor 360 records, consent/restriction enforcement, stewardship recommendations, reviewable drafts, and aggregate engagement analysis.
4. **Partnership intelligence:** policy criteria configuration, public-source research, source-quality assessments, findings register, escalation workflow, and partner-change monitoring.
5. **Proposal and grant intelligence:** document intake, versioning, requirement extraction, proposal critique, logic models, obligation extraction, amendment comparison, and compliance schedules.
6. **Interoperability:** cross-module workflow triggers and shared decision briefs using the evidence and relationship layer.
7. **Operations:** source monitoring worker, production logging/monitoring, backup configuration, and full end-to-end integration tests.

## Required configuration before a live run

- Supabase project URL, anon/publishable key, and service-role key in the server-side `.env` file.
- Gemini Developer API key in the server-side `.env` file.
- Apply `sql/schema.sql` in the Supabase SQL Editor.
- Add the complete approved CSV files to `data/`.
- Run `python scripts/seed_demo_users.py` to create the two synthetic demonstration users.

No live Supabase or Gemini request, Vultr deployment, public demo URL, GitHub push, or recorded demo has been completed as part of this starter milestone.
