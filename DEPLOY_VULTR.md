# Deploy Wai Intelligence on a Vultr VM

This is a simple public web deployment: **one Vultr Ubuntu VM, one FastAPI container, and Caddy for HTTPS**. Supabase hosts the database. Gemini is called by the app when configured. No PostgreSQL server, separate worker VM, Vercel project, or in-app login is required.

## What you need

- A Vultr Cloud Compute VM running Ubuntu 24.04 LTS (2 vCPU / 4 GB RAM is a practical pilot starting point).
- A domain name and access to its DNS settings.
- A Supabase project.
- A Gemini API key if AI-assisted drafting is required.

## 1. Create the VM and point the domain

In Vultr, create an Ubuntu 24.04 VM and add your SSH key. In the Vultr Firewall, allow:

- TCP 22 from your administrator IP only.
- TCP 80 and 443 from the internet.

Do not open port 8000. Create an `A` DNS record for your chosen hostname pointing to the VM's public IPv4 address. Caddy will provide HTTPS automatically after DNS is resolving and ports 80/443 are reachable.

## 2. Install Docker

SSH into the VM and run:

```bash
sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and SSH back in. Confirm `docker --version` and `docker compose version` work.

## 3. Copy the app to the VM

From your computer, copy the extracted `wai-intelligence` project folder:

```bash
scp -r ./wai-intelligence YOUR_VM_USER@YOUR_VM_IP:~/wai-intelligence
```

On the VM:

```bash
cd ~/wai-intelligence
cp .env.example .env
chmod 600 .env
nano .env
```

Set these values in `.env`:

```dotenv
APP_ENV=production
APP_BASE_URL=https://YOUR_DOMAIN
APP_DOMAIN=YOUR_DOMAIN
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=YOUR_SUPABASE_SERVICE_ROLE_KEY
GEMINI_API_KEY=YOUR_GEMINI_API_KEY
GEMINI_MODEL=gemini-3.8-flash
```

Keep the Supabase service-role key only in this server-side `.env` file. Do not put it in browser code or commit `.env` to Git.

## 4. Prepare Supabase

In the Supabase dashboard, open **SQL Editor** and run the complete `sql/schema.sql` file from this project. The app connects to Supabase; the VM does not host a separate PostgreSQL database. The local `data/source` CSV folder is not included in the project archive; create it on the VM only if you intend to import approved CSV files.

## 5. Start the website

On the VM, from `~/wai-intelligence`, run:

```bash
docker compose up -d --build
```

The app and HTTPS proxy start together. The public website is:

```text
https://YOUR_DOMAIN
```

Caddy obtains and renews the HTTPS certificate automatically. The funding-source monitor runs in the FastAPI service when Supabase is configured; there is no separate worker container to set up.

## 6. Check the deployment

```bash
docker compose ps
docker compose logs --tail=100 wai-web caddy
curl -fsS https://YOUR_DOMAIN/health
curl -fsS https://YOUR_DOMAIN/ready
```

`/health` confirms the web process is running. `/ready` confirms the Supabase connection and schema are accessible.

## Updating the website

Copy the reviewed project update to the VM, then run:

```bash
cd ~/wai-intelligence
docker compose up -d --build
```

Uploaded documents persist in the `wai_uploads` Docker volume. Caddy's certificate and configuration persist in its own Docker volumes. Back up the Supabase database and uploaded-document volume separately.

## Important access note

The application intentionally has **no in-app login or logout**. A public website is therefore accessible to anyone who can reach its URL. Keep synthetic or approved non-sensitive data in a public deployment. Do not upload real donor records, personal contact information, or confidential grant documents unless WaterAid has approved the data classification and an external network access restriction for the website.
