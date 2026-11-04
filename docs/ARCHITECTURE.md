# Architecture and workflow boundaries

Wai Intelligence is a server-rendered FastAPI web application. FastAPI handles HTML routes and form submissions; Jinja templates extend the shared `base.html`; static CSS and JavaScript are served from the same app. There is no separately deployed frontend and no API-based client-side routing.

## Runtime components

- **Caddy:** public reverse proxy, HTTPS certificate handling and response security headers.
- **FastAPI:** authentication, role checks, request validation, HTML rendering and workflow orchestration.
- **PostgreSQL 16:** persistent application records on the private Docker network.
- **Source CSV directory:** read-only bind mount configured by `WAI_DATA_DIR`; source rows are not stored in PostgreSQL.
- **Document volume:** persistent uploads for proposal/grant documents.
- **Gemini:** optional outbound LLM service for selected draft/research tasks. Human approval remains mandatory.
- **Public websites:** selected funding/strategy pages checked using the configured source register.

## Five interoperable workflows

| Workflow | Shared records it creates or reads |
|---|---|
| Funding prospecting | Funding opportunities, source checks, evidence, staff decisions and follow-up tasks. |
| Reporting intelligence | Reporting metrics, definitions, period comparisons, review feedback and audience-specific draft narratives. |
| Donor stewardship | Synthetic demonstration profiles for draft workflow testing; read-only constituent source data for operational context; internal follow-up tasks and evidence. |
| Ethical partnership due diligence | Partner cases, source-linked findings, evidence, unresolved questions, decisions and escalation tasks. |
| Proposal and grant compliance | Grant projects, document metadata/versions, candidate obligations, evidence, deadlines and follow-up tasks. |

Evidence, decisions, tasks and feedback are common objects. This lets a finding in one module become a review task without copying it into another system. No CRM, Outlook or Asana integration is present.

## Constituent source-data path

1. `WAI_DATA_DIR` points to the mounted source folder.
2. The source loader reads the four allow-listed CSV files directly, preserving all field values as text and preserving identifier strings.
3. The stewardship source view reads `constituent_profile_table.csv` and exact-matches `CONSTITUENT_ID` to the other three detail files in memory.
4. It displays source-provided profile fields and counts of matched rows. It does not persist a joined record or create a Donor model row.
5. Authenticated CSV exports provide the source files under their original names. The application never writes to the source directory.

`LIFETIME_GIVING` is not assumed to be CAD. `CAPACITY_ESTIMATE` is labelled source-provided and is not independently inferred. The sample profile records do not supply constituent names or communication preferences; the draft-writing demonstration therefore remains separate and uses fictional profiles.

## Security and operational boundaries

- PostgreSQL is the only supported runtime database. It is on the private Compose network and should not have a public firewall rule.
- Caddy is the only service publishing ports 80/443. FastAPI's port 8000 is internal.
- Source data is a read-only container mount. Production CSVs should not be included in public Git repositories or Docker image layers.
- Uploaded documents persist in a dedicated Docker volume. Grant-document CSV exports omit extracted text and filesystem paths.
- Passwords are PBKDF2-SHA256 hashed; sessions are signed; roles and account activity are checked on requests; mutating forms require CSRF tokens.
- No outbound donor emails, calendar events, CRM writes, Asana tasks, proposals or partner decisions are sent automatically.
- The due-diligence test criteria are assumptions until the approved WaterAid ethical partnership framework is supplied.

The Vultr deployment topology and operational steps are documented in [VULTR_DEPLOYMENT.md](VULTR_DEPLOYMENT.md).
