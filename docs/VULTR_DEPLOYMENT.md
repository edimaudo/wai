# Wai Intelligence — deploy the web app on Vultr

This guide deploys the existing GitHub application to one Vultr VM. **You do not need to buy a domain, register a DNS service, or host a separate frontend.** Staff open the website using the VM's public IPv4 address:

`https://YOUR_VULTR_PUBLIC_IP`

The deployment is:

- GitHub repository: `https://github.com/edimaudo/wai`
- One Vultr Cloud Compute VM running Ubuntu 24.04
- Docker Compose running FastAPI, PostgreSQL 16 and Caddy
- Caddy obtains and renews a publicly trusted HTTPS certificate for the VM's IPv4 address
- PostgreSQL and uploaded documents persist in Docker volumes
- Production CSV files are stored outside the Git checkout and mounted read-only into the app

FastAPI serves the HTML pages and backend actions. There is no separately deployed frontend.

## Before starting

You need:

- A Vultr account.
- A computer with Terminal (macOS/Linux) or PowerShell (Windows).
- An SSH key added to Vultr when you create the VM.
- The four approved CSV source files, if you are ready to use the real datasets.
- A Gemini API key only if you want the Gemini-powered functions enabled. It is optional.

The GitHub repository is public and contains sample data. **Do not commit real constituent or donor data to GitHub.** This guide stores the operational CSVs in `/opt/wai-data/source`, outside the application repository.

## 1. Create the Vultr VM

In the [Vultr control panel](https://my.vultr.com/):

1. Open **Products → Compute** and choose **Deploy Instance**.
2. Select **Cloud Compute**.
3. Choose **Ubuntu 24.04 LTS, 64-bit**.
4. For an initial small-team deployment, start with at least **2 vCPUs and 4 GB RAM**. This is a starting size, not a measured capacity guarantee.
5. Select a region approved by your organization for data residency and operations.
6. Add your SSH public key to the instance.
7. Deploy the VM.
8. Copy the instance's **public IPv4 address**. Call it `VULTR_PUBLIC_IP` in the commands below.

Use the VM's public IPv4 address. Do not create a DNS record or purchase a domain.

Vultr reference: [Cloud Compute](https://docs.vultr.com/products/compute/instances/cloud-compute).

## 2. Set the Vultr firewall

In **Products → Network → Firewall**, create a firewall group and attach it to the VM. Add these inbound rules:

| Protocol | Port | Source |
|---|---:|---|
| TCP | 22 | Your current public IP only (use an IPv4 /32 rule) |
| TCP | 80 | Any IPv4 address |
| TCP | 443 | Any IPv4 address |

Port 80 is needed for Let's Encrypt's HTTP certificate validation. Port 443 serves the website over HTTPS.

Do not open ports 8000 or 5432. FastAPI and PostgreSQL are not intended to be directly accessible from the public internet. UDP 443 is not required for this setup.

## 3. Connect to the VM

Open Terminal or PowerShell on your computer:

```bash
ssh root@VULTR_PUBLIC_IP
```

Replace `VULTR_PUBLIC_IP` with the address shown in Vultr. Accept the SSH host fingerprint only after confirming that this is the VM you created.

The commands below run inside this SSH session as `root`.

## 4. Install Git and Docker

Run inside the VM:

```bash
apt-get update
apt-get -y upgrade
apt-get install -y ca-certificates curl git openssl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
cat > /etc/apt/sources.list.d/docker.sources <<DOCKERREPO
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
DOCKERREPO

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

docker --version
docker compose version
```

These commands install Docker Engine and the Docker Compose plugin from Docker's official Ubuntu package repository.

Official reference: [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/).

## 5. Download the application directly from GitHub

Run inside the VM:

```bash
mkdir -p /opt
git clone --branch main https://github.com/edimaudo/wai.git /opt/wai
cd /opt/wai
```

This downloads the application source code from the `main` branch. You are not uploading a ZIP or manually transferring the application code.

Confirm the expected files exist:

```bash
ls -la
```

You should see `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `.env.example`, `.dockerignore`, `app/` and `docs/`.

## 6. Put the source CSV files outside the Git repository

Create a private data folder on the VM:

```bash
mkdir -p /opt/wai-data/source
chown -R 10001:10001 /opt/wai-data/source
chmod 750 /opt/wai-data/source
```

From a **second Terminal/PowerShell window on your own computer**, copy the approved CSV files to the VM. Replace the local folder and the VM IP:

```bash
scp /path/to/your/csvs/constituent_profile_table.csv \
    /path/to/your/csvs/gift_transactions_table.csv \
    /path/to/your/csvs/CRM_interacions_table.csv \
    /path/to/your/csvs/video_email_data_table.csv \
    root@VULTR_PUBLIC_IP:/opt/wai-data/source/
```

Back in the VM, set permissions:

```bash
chown -R 10001:10001 /opt/wai-data/source
chmod 750 /opt/wai-data/source
find /opt/wai-data/source -maxdepth 1 -type f -name '*.csv' -exec chmod 640 {} \;
ls -lh /opt/wai-data/source
```

The expected filenames are:

- `constituent_profile_table.csv`
- `gift_transactions_table.csv`
- `CRM_interacions_table.csv`
- `video_email_data_table.csv`

If the approved CSVs are not ready and you want to test the sample rows, copy the repository samples into the external data folder on the VM:

```bash
cp /opt/wai/data/source/*.csv /opt/wai-data/source/
chown -R 10001:10001 /opt/wai-data/source
chmod 750 /opt/wai-data/source
find /opt/wai-data/source -maxdepth 1 -type f -name '*.csv' -exec chmod 640 {} \;
```

Do not upload incomplete or reconstructed records and do not put real donor data in GitHub.

Docker Compose mounts `/opt/wai-data/source` into the application container as read-only. The source rows are read from those files; they are not imported into PostgreSQL.

## 7. Configure the production environment

Inside the VM:

```bash
cd /opt/wai
cp .env.example .env
openssl rand -hex 32
openssl rand -hex 24
```

The first output is your application session secret. The second is your PostgreSQL password. Use the generated values; do not use the example values.

Open the environment file:

```bash
nano .env
```

Set these values, replacing the examples with the actual values:

```dotenv
ENVIRONMENT=production
SECRET_KEY=PASTE_FIRST_RANDOM_VALUE
POSTGRES_PASSWORD=PASTE_SECOND_RANDOM_VALUE
SERVER_IP=VULTR_PUBLIC_IP
WAI_HOST_DATA_DIR=/opt/wai-data/source
SESSION_HTTPS_ONLY=true
DEMO_SEED_USERS=false
DEMO_SEED_DATA=false
GOOGLE_API_KEY=
GEMINI_MODEL=gemini-3.5-flash
```

- Set `SERVER_IP` to the exact public IPv4 address shown in the Vultr control panel. Do not add `https://`, a port or a trailing slash.
- Keep `WAI_HOST_DATA_DIR=/opt/wai-data/source`. This keeps production CSVs outside the Git checkout.
- Add your Gemini API key to `GOOGLE_API_KEY` only if you have one. It is optional.
- Leave the other values from `.env.example` unchanged unless you have a specific reason to change them. Docker Compose sets the internal PostgreSQL connection string for the application.

Save the file, then restrict access:

```bash
chmod 600 .env
```

The two demo switches must be false in production. This prevents the application from creating demonstration users and fictional workflow records in the new production database. It does not delete existing database records.

## 8. Validate the configuration and start Wai

Run inside `/opt/wai`:

```bash
docker compose config --quiet
docker compose pull db caddy
docker compose run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose up -d --build
docker compose ps
```

If `docker compose config --quiet` reports a missing variable or configuration error, correct `.env` before starting the services. The Caddy validation command must also report that the configuration is valid; fix any Caddyfile error before continuing.

You should see three services:

- `app`: FastAPI website and backend.
- `db`: PostgreSQL 16 database.
- `caddy`: public HTTPS entry point.

Check the startup logs:

```bash
docker compose logs --tail=100 db app caddy
```

The first start builds the FastAPI application image and creates the PostgreSQL database. Caddy then requests a public TLS certificate for the server IP. Certificate issuance happens automatically and may take a short time.

## 9. Open the website

Wait for Caddy's logs to show that the certificate was obtained successfully:

```bash
docker compose logs caddy
```

Then, in your browser, open:

```text
https://VULTR_PUBLIC_IP
```

Use the exact IPv4 address from Vultr. The website should load with a valid HTTPS connection.

You can also check the health endpoint from your own computer:

```bash
curl -fsS https://VULTR_PUBLIC_IP/healthz
```

Expected response:

```text
ok
```

If a certificate is not issued, check that the IP in `.env` exactly matches the VM's public IPv4 address, that inbound TCP ports 80 and 443 are allowed, and that Caddy is running. See Troubleshooting below.

## 10. Create the first administrator account

Inside the VM:

```bash
cd /opt/wai
docker compose exec app python -m app.scripts.create_user \
  --username wai-admin --role admin --display-name "Wai Administrator"
```

Enter and confirm a unique password of at least 14 characters when prompted.

Create a staff account in the same way:

```bash
docker compose exec app python -m app.scripts.create_user \
  --username fundraising-lead --role user --display-name "Fundraising Lead"
```

Do not use the demonstration credentials from local development.

## 11. Check the application

Open `https://VULTR_PUBLIC_IP` and verify:

| Area | Check |
|---|---|
| Website | Landing page loads over HTTPS without a certificate warning |
| Login | Sign in with the administrator account |
| Access | Administrator dashboard opens; staff account has normal user access |
| Language | Switch between Canadian English and Canadian French; navigate between pages |
| Source data | Constituent Source Data detects the CSV files in the mounted folder |
| Exports | Authenticated CSV downloads work |
| Workflows | Funding prospecting, reporting, donor stewardship, due diligence and grant compliance pages open |
| Health | `https://VULTR_PUBLIC_IP/healthz` returns `ok` |

These are deployment acceptance checks. They do not replace functional testing of every workflow or a formal accessibility audit.

## 12. Deploy future GitHub updates

The application code lives in GitHub. When you are ready to deploy a new version:

```bash
cd /opt/wai
git pull --ff-only origin main
docker compose pull db caddy
docker compose up -d --build
docker compose ps
```

That is the normal update process: push changes to GitHub, then pull them onto the VM and rebuild the application.

The production `.env` file, PostgreSQL volume, uploaded documents volume and `/opt/wai-data/source` folder are separate from the Git checkout. Do not delete them when updating the code.

This is a manual Git pull deployment. The repository does not currently configure an automatic GitHub Actions deployment to Vultr.

## 13. Back up production data

Before using real constituent or grant information, set up an encrypted backup location outside the Vultr VM and agree on backup frequency and retention.

A PostgreSQL backup can be created on the VM with:

```bash
mkdir -p /opt/wai-backups
chmod 700 /opt/wai-backups
cd /opt/wai
docker compose exec -T db pg_dump -U wai_app -d wai_intelligence \
  > "/opt/wai-backups/postgres-$(date +%F-%H%M).sql"
```

Copy the backup to your organization's approved encrypted storage. Back up the source CSV files and uploaded grant documents separately. Keep the `.env` file in an approved secrets/password manager; it contains production secrets.

**Do not run `docker compose down -v` as a routine restart or update command.** The `-v` option deletes the persistent database, uploaded-document and Caddy certificate volumes.

## Troubleshooting

| Problem | What to check |
|---|---|
| SSH times out | Confirm the VM is active, the IP is correct and TCP 22 allows your current public IP. |
| Website does not open | Confirm the firewall allows inbound TCP 80 and 443, then run `docker compose ps`. |
| Caddy cannot obtain the IP certificate | Confirm `SERVER_IP` exactly matches the VM's public IPv4 address; port 80 must be reachable from the internet. Inspect `docker compose logs caddy`. |
| Browser reports a certificate error | Wait for Caddy to complete certificate issuance. Do not bypass the warning for normal staff use. Check Caddy logs and firewall rules. |
| FastAPI is restarting | Run `docker compose logs --tail=200 app`. Production requires a unique `SECRET_KEY`, PostgreSQL, and both demo switches set to false. |
| Database is not ready | Run `docker compose ps db` and `docker compose logs db`. Check `POSTGRES_PASSWORD` in `.env`. |
| CSV files are not detected | Check the exact filenames, `WAI_HOST_DATA_DIR` and permissions on `/opt/wai-data/source`. |
| 502 Bad Gateway | Caddy is running but cannot reach FastAPI. Check `docker compose ps` and `docker compose logs app caddy`. |
| A Git pull conflicts with data | Production CSV files must be stored in `/opt/wai-data/source`, not in the repository's `data/source` folder. |

## Why an IP address works without a domain

Let's Encrypt now issues short-lived certificates for public IP addresses. These certificates are valid for 160 hours and must use the `shortlived` ACME profile. The Caddy configuration in this repository requests that profile and disables the TLS-ALPN challenge so validation uses HTTP on port 80. Caddy stores certificate state in the persistent `caddy_data` volume and renews certificates automatically.

This is why the deployment needs the VM's public IPv4 address and open TCP ports 80 and 443, but does not need a domain or DNS provider. If the VM's public IP changes, update `SERVER_IP` in `.env` and recreate the Caddy service.

References:

- [Let's Encrypt: IP address and six-day certificates](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)
- [Caddy releases](https://github.com/caddyserver/caddy/releases)
- [Vultr Cloud Compute](https://docs.vultr.com/products/compute/instances/cloud-compute)
- [Docker Engine installation on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
