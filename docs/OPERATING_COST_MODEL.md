# Wai Intelligence — operating cost model

This is a measurement framework for a small-team pilot, not a vendor quote or forecast. Actual cloud, model and staff rates must be entered after deployment and procurement approval.

## Cost components

| Component | Measurement | Cost treatment |
|---|---|---|
| Vultr VM | VM plan, storage, snapshots, bandwidth and backup usage | Monthly invoice, allocated to the application. |
| Database and document storage | PostgreSQL storage, uploaded document volume, backup retention | Monthly storage and backup charges. |
| Public-source monitoring | Number of source checks, response size, failures, frequency and maintenance | HTTP checks are inexpensive relative to staff review; record any paid data-source subscription separately. |
| Gemini | Input/output tokens by workflow and model | Calculate from actual model usage and current contracted pricing. Keep separate for prospecting, reporting, stewardship, due diligence and grant review. |
| Staff review | Time to verify, correct and approve an output | Reviewer minutes × fully loaded hourly rate. Include review and rework, not only generation time. |
| Maintenance and governance | Source updates, prompt/model evaluation, access reviews, incident response and policy changes | Monthly owner hours × fully loaded rate. |

## Workflow-level calculation

- **Funding prospecting:** source monitoring + model interpretation of changed pages (only when enabled) + staff review minutes per qualified lead + follow-up research. Measure precision: proportion of surfaced leads that staff judge relevant and eligible enough to investigate.
- **Reporting:** dataset preparation + metric calculations + narrative-generation tokens + staff context/review/correction time. Compare total cycle time with the previous reporting process and separately track correction rate and unsupported-claim rate.
- **Donor stewardship:** retrieval of separately approved stewardship records + draft-generation tokens + staff verification and editing time per message. Include privacy and preference checks. The supplied source CSVs are read-only files and are not imported into donor profiles. No CRM, Outlook or Asana connector costs apply because those integrations are not part of the application. Follow-up work remains internal. No benefit should be attributed to donor retention or increased giving from this demonstration alone.
- **Ethical due diligence:** public-source research and retrieval + synthesis tokens + source verification + staff analysis/escalation time. Track coverage and unresolved questions; do not equate low search volume with low risk.
- **Proposal and grant compliance:** document storage and extraction + review tokens + clause-by-clause validation + grant-manager/finance/programme time. OCR, automatic legal interpretation and cross-document redlining are not included in this version.

## Demonstration assumptions already shown in the app

- Source monitoring cost: **CAD 35/month** placeholder.
- Staff review: **20 minutes per lead** placeholder.
- The values are editable assumptions, not actual operating costs or a quote. They exclude the complete Vultr, database, model, maintenance and governance costs above.

## Break-even and value test

For each workflow, record the current baseline and the pilot result:

`Net staff time saved = baseline preparation + baseline review − (AI-assisted preparation + AI-assisted review + correction)`

`Net pilot cost = infrastructure + storage + source services + model usage + staff verification + maintenance`

`Break-even value = incremental programme / fundraising contribution attributable to the workflow − net pilot cost`

The fit score is not a win probability. When WaterAid historical application and award outcome data are unavailable, the application may show a clearly labelled, transparent heuristic based on fit, eligibility confidence and timing. It is a directional prioritization aid only and is not a calibrated probability or funding forecast. It shows N/A with a reason when a deadline has passed or a critical applicant-eligibility/invitation gate is unresolved. Calibration requires comparable historical applications, eligibility decisions, submissions, award outcomes, amounts, time-to-decision and reasons for success or failure. Until those data are approved and validated, evaluate prospecting by relevant-lead precision, qualified opportunities found, staff time per qualified lead and documented progression through the pipeline.

## Pilot measurement plan

Capture, per workflow: number of records processed; generation latency; model token use; staff review minutes; correction count; factual error count; source-verification completion; number of outputs approved, rejected or escalated; and staff usefulness rating. Compare at least several reporting cycles or funding review cycles with a baseline. Include rework and maintenance so that apparent generation-time savings are not overstated.
