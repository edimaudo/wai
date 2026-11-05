# Wai Intelligence

Wai Intelligence is a unified, server-rendered FastAPI application for WaterAid Canada's funding, reporting, donor stewardship, partnership due diligence, and proposal/grant workflows.

The initial implementation establishes the application shell, Supabase authentication and role checks, the shared schema, administrative access controls, private CSV discovery/import, bilingual navigation, theme/font preferences, and module entry points. The five module pages are deliberately presented as foundations until their full workflows are implemented; the application does not generate fabricated opportunity, donor, risk, or reporting results.

## Technology

- Python 3.12+
- FastAPI and Jinja2 server-rendered web pages
- Vanilla JavaScript and responsive CSS
- Supabase Auth and PostgreSQL with Row Level Security
- Gemini 3.6 Flash through Google's Gemini Developer API and `google-genai`
- Plotly.js is reserved for the reporting visualization phase
- Vultr Cloud Compute for hosting

The application uses ordinary web routes. It does not expose an `/api` route structure or use a frontend router.

## Project structure

```text
app/
  main.py                  FastAPI application
  web.py                   HTML routes and form handlers
  security.py              Supabase session validation and role checks
  supabase_client.py       Request-scoped user clients and server admin client
  services/
    csv_ingestion.py       Read-only CSV validation and Supabase imports
    gemini.py              Gemini 3.6 Flash service boundary
    modules.py             Five shared-workspace module definitions
  templates/               Jinja2 pages; every page extends base.html
  static/                  CSS and JavaScript

data/                      Private source-file directory
  dictionaries/             Data dictionary and metric definitions
sql/schema.sql              Initial Supabase schema and RLS policies
scripts/seed_demo_users.py  Create local/demo admin and staff accounts
```

## 1. Configure Supabase

1. Create a Supabase project.
2. Open **SQL Editor** in the Supabase dashboard.
3. Run the complete contents of `sql/schema.sql`.
4. In **Project Settings → API**, copy the project URL, publishable/anon key, and service-role key.
5. Keep the service-role key exclusively on the FastAPI server. Never put it in HTML, JavaScript, a public repository, or a browser environment variable.

The SQL creates the profile trigger, shared tables, indexes, and Row Level Security policies. Application users can read records only when their profile is active. Direct browser-side writes are restricted; privileged imports and account administration run through protected FastAPI handlers.

## 2. Configure environment variables

```bash
cp .env.example .env
```

Set the following values in `.env`:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`
- `GEMINI_API_KEY`
- `APP_SECRET_KEY` (generate a long random value before deployment)

Keep `GEMINI_MODEL=gemini-3.6-flash` as requested. The code uses the Gemini Developer API endpoint through `google-genai`, not the separate Gemini Enterprise Agent Platform endpoint.

For local development, `APP_SESSION_COOKIE_SECURE=false` is appropriate for `http://localhost`. Set it to `true` behind HTTPS in production.

## 3. Install and run locally

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://localhost:8000`.

The public landing page and sign-in screen work without Supabase. Protected application routes require Supabase configuration and an active profile.

## 4. Create demonstration accounts

The seed script creates:

- `admin@wai-intelligence.test` with the `admin` role
- `staff@wai-intelligence.test` with the `staff` role

Set `DEMO_ADMIN_PASSWORD` and `DEMO_STAFF_PASSWORD` in `.env` before running the script. When either is blank, the script generates a random password. The credentials are written to `.demo-credentials.txt` with restrictive local file permissions; that file is in `.gitignore`.

```bash
python scripts/seed_demo_users.py
```

The script is intended for synthetic demonstration data. Do not use these accounts or passwords for a real WaterAid production environment. For production, create individual accounts and enforce the organization's identity and access policies.

## 5. Add source CSV files

Put approved CSVs in the private `data/` directory. The first import recognizes:

- `gift_transactions_table.csv`
- `CRM_interacions_table.csv` (the spelling supplied in the project brief)
- `CRM_interactions_table.csv` (corrected spelling is also accepted)
- `video_email_data_table.csv`

The importer can discover these files in subfolders as well. The original files are never modified. The Docker container mounts `./data` at `/app/data` as read-only.

The importer:

1. Checks required column names.
2. Normalizes date, number and boolean fields.
3. Treats `NA`, `N/A`, `NULL` and empty cells as missing, not zero.
4. Rejects invalid rows and records a limited list of validation messages.
5. Uses content hashes to avoid re-importing an identical file version.
6. Upserts normalized records into Supabase and records an import-history row.

A changed file version is imported as an upsert. Records removed from a later source file are not automatically deleted from Supabase; reconcile those changes explicitly to preserve auditability.

The supplied sample snippets are not included as fabricated CSV files. Add the complete approved files before importing.

## 6. Routes

| Route | Access |
|---|---|
| `/` | Public landing page |
| `/login` | Public sign-in |
| `/logout` | CSRF-protected sign-out form |
| `/app` | Authenticated dashboard |
| `/app/funding` | Authenticated |
| `/app/reporting` | Authenticated |
| `/app/donors` | Authenticated |
| `/app/partnerships` | Authenticated |
| `/app/grants` | Authenticated |
| `/app/reviews` | Authenticated |
| `/app/admin` | Administrator |
| `/app/admin/access` | Administrator |
| `/app/admin/data` | Administrator |
| `/health` | Deployment health check |

Every HTML page extends `base.html`. The public landing page does not display internal application links. Route-level authentication and role checks protect the pages on the server.

## 7. Run with Docker

```bash
cp .env.example .env
# Configure .env first.
docker compose up --build -d
docker compose logs -f wai-web
```

The service binds to `127.0.0.1:8000` on the VM. Put Nginx in front of it and terminate HTTPS at Nginx. Do not expose the application port directly to the public internet. A starting Nginx server-block is provided in `deploy/nginx/wai-intelligence.conf.example`; replace the example host name and certificate paths before enabling it. The example also applies a basic login request limit.

## 8. Vultr deployment outline

1. Provision an Ubuntu LTS Vultr VM. The initial planning size is 2 vCPU and 4 GB RAM; adjust based on actual concurrency and document-processing load.
2. Configure SSH key authentication and firewall rules. Allow inbound SSH from trusted addresses and HTTPS on ports 80/443; keep port 8000 private on loopback.
3. Install Docker Engine and the Docker Compose plugin.
4. Clone the private application repository onto the VM.
5. Create `.env` on the VM and set production values, including `APP_ENV=production` and `APP_SESSION_COOKIE_SECURE=true`.
6. Put the approved source files in the VM's private `data/` directory and restrict filesystem permissions.
7. Apply `sql/schema.sql` to Supabase and provision individual application accounts.
8. Run `docker compose up --build -d`.
9. Configure Nginx and TLS for the public domain, then test `/health`, sign-in, role restrictions, and logout.
10. Configure VM monitoring, log rotation, backups and a patching schedule.

Do not place `.env`, donor source files, or `.demo-credentials.txt` in Git.

## Security and operational notes

- Access and refresh tokens are stored in HttpOnly cookies.
- Forms use a double-submit CSRF token.
- Supabase verifies access tokens on protected requests; account role and active status are checked against `profiles`.
- User-facing data queries use a request-scoped client carrying the authenticated user's JWT so Supabase RLS remains in force.
- The service-role client is server-only and reserved for account administration, imports, and audited privileged operations.
- No Outlook or Asana integration is included.
- The app does not expose the local `data/` folder as static content.
- Do not import real donor data until privacy, retention, consent, role access and hosting approvals are completed.
- Gemini output is draft decision support. Validate extracted facts and route consequential decisions and external communications for human review.

## Validation

```bash
pip install -r requirements-dev.txt
python -m compileall app scripts main.py
pytest -q
```

The tests cover the filename recognition and CSV normalization rules. Full integration tests require a configured Supabase project and test users.
