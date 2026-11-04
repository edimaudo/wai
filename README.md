# Wai Intelligence

Wai Intelligence is a bilingual FastAPI decision-support workspace for WaterAid Canada. It connects five staff-reviewed workflows through a shared evidence register, decisions, follow-up tasks and feedback records.

## Product capabilities

1. **Funding prospecting** — tracks selected Canadian public funder sources, summarizes funding pathways, scores fit against editable criteria, records staff decisions and surfaces eligibility/deadline uncertainties.
2. **Reporting intelligence** — compares synthetic fundraising, donor and engagement measures; provides source-linked charts and audience-specific draft narratives.
3. **Donor stewardship** — uses synthetic donor histories, preferences and restrictions to prepare reviewable briefs and communication drafts. Follow-up tasks remain inside Wai Intelligence.
4. **Ethical partnership due diligence** — organizes public-source findings and fictional cases, distinguishes allegations from investigations and formal findings, and leaves Go/No-Go decisions with authorized staff.
5. **Proposal and grant compliance** — reviews uploaded proposal and grant documents, records candidate obligations, tracks versions and identifies document, clause and deadline bottlenecks.

The work areas share evidence, internal tasks, reviewer feedback and documented relationships. A decision in one workflow can create follow-up work in another while preserving its evidence trail.

## Integrations and data boundaries

There are **no CRM, Outlook, Asana, email-sending, calendar-sync or external task-management integrations**. The three CSV files supplied in the project brief live in `data/source/` and are read directly from that folder through `WAI_DATA_DIR`. They are opened read-only for source inspection and CSV export; their rows are **not imported into the application database**, copied into donor profiles, or automatically joined to other records. The app never modifies the source files. The stewardship screen uses separate, clearly labelled fictional demonstration profiles because the supplied CSV schemas do not contain donor names, communication preferences or consent restrictions. Tasks and reminders are maintained within Wai Intelligence.

Gemini is used only for optional draft and analysis assistance when `GOOGLE_API_KEY` is configured. The model never approves an opportunity, donor communication, partnership or grant obligation. Outputs require staff review. The configured model ID is `gemini-3.5-flash` and can be changed with `GEMINI_MODEL`.

## Language support

The application interface is available in Canadian English (`en-CA`) and Canadian French (`fr-CA`), including localized date formats, dynamic status labels and validation messages. The selected language is preserved during sign-in. AI prompts request the selected language for generated drafts and source-monitoring summaries. Official funder names, source URLs, uploaded source documents and original source CSV content are preserved in their original language; they are not silently translated or altered.

## Data and CSV exports

Place approved source CSV files in `data/source/` or configure `WAI_DATA_DIR` to another readable folder. The loader does not modify source files and treats constituent/contact identifiers as strings.

The three source files supplied in the brief are included in `data/source/`:

- `gift_transactions_table.csv`
- `CRM_interacions_table.csv` (filename preserved as supplied; interaction field names are unchanged)
- `video_email_data_table.csv`

The reporting demonstration metrics are a separate synthetic dataset seeded into the reporting workspace. They are not presented as a fourth user-supplied source file.

Authenticated staff can open **Data exports** in the workspace and download each dataset separately, or download a ZIP containing all available CSV datasets. Exports include opportunities, separate fictional stewardship demo profiles, reporting measures, partner cases, evidence, internal tasks, public source settings, grant project/document metadata, obligations, staff feedback and the three source CSV files. The source CSVs are downloaded from the configured folder; the other datasets are workspace records. Grant document exports intentionally omit extracted document text and server file paths. CSV values are serialized with formula-injection protection. CSV field names remain stable in English for interoperability with analysis tools; the UI and export descriptions are bilingual.

CSV export URLs:

- `/app/exports` — export centre
- `/app/exports/{dataset}.csv` — one dataset
- `/app/exports/all.zip` — all available datasets in one ZIP

All export routes require an authenticated session. Exports may contain sensitive donor or operational information and must be stored only in approved, access-controlled locations.

## Local setup

Requirements: Python 3.12+.

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

### Local demonstration accounts

| Role | Username | Password |
|---|---|---|
| Administrator | `admin` | `WaiAdmin!2026` |
| Staff user | `fundraiser` | `WaiUser!2026` |

These credentials are seeded for local evaluation only. Replace them and turn off demo seeding before any public deployment.

## Configuration

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | Set to `production` for production safety checks. |
| `SECRET_KEY` | Long, random session-signing secret. |
| `DATABASE_URL` | SQLite locally; PostgreSQL for the Vultr deployment. |
| `WAI_DATA_DIR` | Folder containing approved source CSVs. |
| `UPLOAD_DIR` | Persistent location for uploaded documents. |
| `GOOGLE_API_KEY` | Optional Gemini API credential. |
| `GEMINI_MODEL` | Defaults to `gemini-3.5-flash`. |
| `SESSION_HTTPS_ONLY` | Set `true` behind HTTPS in production. |
| `DEMO_SEED_USERS` | Set `false` in production; provision named users separately. |

## Vultr deployment

The repository includes a Dockerfile, Docker Compose configuration with PostgreSQL, Caddy reverse proxy and persistent document/database volumes. Before starting:

1. Create a Vultr VM and point the domain's DNS A/AAAA records to it.
2. Install Docker Engine and the Docker Compose plugin.
3. Copy the repository to the VM.
4. Create `.env` with a unique `SECRET_KEY`, strong `POSTGRES_PASSWORD`, valid `DOMAIN`, `ENVIRONMENT=production`, `SESSION_HTTPS_ONLY=true`, and `DEMO_SEED_USERS=false`.
5. Set `GOOGLE_API_KEY` only if Gemini assistance is required.
6. Start with `docker compose up -d --build` and verify `https://your-domain/healthz`.
7. Provision named administrator and staff accounts using the documented account-management process before disabling demo seeding.

Do not deploy with the demonstration credentials or the example session secret. Restrict SSH, apply OS updates, configure backups and monitor disk usage. This package has not itself provisioned a Vultr VM or public domain.

## Tests

Run:

```bash
python -m pytest -q
```

The test suite covers authentication, role restrictions, CSV loading without source mutation, exports, heuristic/N/A win-probability behaviour, bilingual session handling and key accessibility semantics.

## Accessibility

The interface is engineered toward **WCAG 2.2 Level AA**. Implemented measures include semantic page landmarks, skip navigation, programmatic labels, visible keyboard focus, keyboard-operable native controls, responsive layouts, text scaling, reduced-motion support, high-contrast focus indicators, forced-colours support, chart data tables and an explicit document language. See [`docs/ACCESSIBILITY_AUDIT.md`](docs/ACCESSIBILITY_AUDIT.md).

Automated route/DOM checks are not a formal conformance certification. Before production acceptance, complete the manual keyboard, screen-reader, zoom/reflow and contrast verification listed in the audit document with the actual deployed browser/assistive-technology combinations.

## Win-probability approach

The fit score and win-probability estimate are different measures. No historical WaterAid application/award outcome dataset was supplied. Where the applicant eligibility gate is sufficiently clear, the prospecting view therefore displays a **heuristic estimate**, labelled as such, based on the weighted fit score and transparent eligibility/timing adjustments. It is bounded and intended only to prioritize staff review; it is not a calibrated probability, prediction or funding claim. The view displays **N/A with a reason** when a deadline has passed, eligibility information is missing, or applicant type, invitation status or another critical eligibility condition remains unresolved. Historical application, eligibility, submission, decision and award data are required before the heuristic can be replaced by a calibrated model.

## Important demonstration limits

- The three source CSVs contain only the sample rows supplied in the project brief. The donor stewardship profiles and reporting metrics shown elsewhere in the app are separate fictional demonstration records; source CSV rows are not imported or joined to them.
- The supplied ethical standards PDF was not available during the build; due-diligence criteria in the demonstration are assumptions, not WaterAid policy.
- Source monitoring checks configured pages for availability/change; it does not prove that an opportunity is open or that WaterAid is eligible.
- Document extraction is text-based. Scanned-document OCR and clause-by-clause redlining are not included.
- CRM, Outlook and Asana connectors are intentionally absent.
- This application is a demonstration starter, not an approved production system for sensitive constituent records.
