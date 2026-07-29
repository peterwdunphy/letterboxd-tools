# Deploying the beta

Matches how the droplet is actually set up (inspected 2026-07-28):

- **No nginx, no certbot.** Public traffic arrives through a **Cloudflare
  Tunnel** (`cloudflared.service`, tunnel name `amc-api2`), configured in
  `/etc/cloudflared/config.yml`. It already maps
  `amc-api.peterwdunphy.com → http://localhost:8787`. TLS and DNS are handled
  by Cloudflare, so adding an API is just another ingress rule.
- Apps live in `/root/apps/`. The AMC server runs as a bare
  `python3 -u server.py` (no service unit), so it would not survive a reboot.
  This one runs under systemd instead.
- Python 3.12.3, `venv` available, 21 GB free.

Everything below runs on the droplet unless it says "on your Mac".

## 1. Copy the code up (on your Mac)

Keep the repo layout: `config.py` looks for `.env` two directories above
itself, so the code must sit at `<root>/backend/` with `.env` at `<root>/.env`.

```bash
cd "/Users/peterdunphy/Library/CloudStorage/Dropbox/misc_research/random/letterboxd_tools"

# code only — venv, cache, and .env are excluded
rsync -av --exclude '.venv' --exclude '__pycache__' --exclude 'data' \
      --exclude '.git' --exclude '.env' \
      ./ do:/root/apps/letterboxd-tools/

# secrets, copied separately and never committed
scp .env do:/root/apps/letterboxd-tools/.env
```

## 2. Install dependencies

```bash
ssh do
cd /root/apps/letterboxd-tools
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
```

`curl_cffi` ships prebuilt wheels for Linux x86-64, so this should not need a
compiler. If pip tries to build from source, `apt install build-essential
libffi-dev` first.

## 3. Smoke test before wiring anything up

Confirms the two risky things at once: that Letterboxd serves the droplet, and
that the TMDB credentials came across.

> **Why this matters (learned the hard way, 2026-07-28):** which browser TLS
> fingerprint Cloudflare accepts depends on the client's IP reputation. From a
> home connection a Chrome fingerprint works; from this droplet's datacenter IP,
> Chrome and Firefox get challenged on the diary and deep pagination while
> **Safari passes cleanly**. `letterboxd.py` now walks an `IMPERSONATE_CHAIN`
> and sticks with the first profile that works, so both environments are handled
> automatically. If Letterboxd ever tightens further, add newer profiles to that
> tuple — `curl_cffi` ships targets like `safari260`, `chrome146`, `firefox147`.

```bash
cd /root/apps/letterboxd-tools/backend
../.venv/bin/python -c "
from lbtools import letterboxd as l
d = l.get_diary('peterdunphy', max_pages=1)
print('diary OK:', len(d), 'entries, newest:', d[0].title, d[0].watched_date)"

../.venv/bin/python -c "
from lbtools import tmdb
print('TMDB OK:', tmdb.enrich(__import__('lbtools.cache', fromlist=['x']).connect(), 680)['title'])" \
  2>/dev/null || ../.venv/bin/python -m lbtools profile peterdunphy 2>&1 | tail -3
```

If the diary line prints entries with dates, the TLS-fingerprint scraping works
from the droplet.

## 4. Run it as a service

`/etc/systemd/system/boxd-api.service`:

```ini
[Unit]
Description=Letterboxd Tools API
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/apps/letterboxd-tools/backend
EnvironmentFile=/root/apps/letterboxd-tools/.env
Environment=BETA_PASSWORD=filmwhore
ExecStart=/root/apps/letterboxd-tools/.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8010
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

Port 8010 is free (8787 is the AMC server). Binding to `127.0.0.1` means only
the tunnel can reach it, never the open internet.

```bash
systemctl daemon-reload
systemctl enable --now boxd-api
systemctl status boxd-api --no-pager
curl localhost:8010/api/health          # expect {"ok":true,"jobs":0}
```

## 5. Publish it through the existing tunnel

Edit `/etc/cloudflared/config.yml` and add the new hostname **above** the
catch-all 404 rule (order matters, first match wins):

```yaml
tunnel: amc-api2
protocol: http2
credentials-file: /root/.cloudflared/ce428c0e-0656-4c2d-ad1c-d4af87c9d256.json
ingress:
  - hostname: amc-api.peterwdunphy.com
    service: http://localhost:8787
  - hostname: boxd-api.peterwdunphy.com      # new
    service: http://localhost:8010           # new
  - service: http_status:404
```

Create the DNS record (Cloudflare adds a CNAME to the tunnel for you), then
reload:

```bash
cloudflared tunnel route dns amc-api2 boxd-api.peterwdunphy.com
systemctl restart cloudflared
systemctl status cloudflared --no-pager | head -5
```

Verify from your Mac:

```bash
curl https://boxd-api.peterwdunphy.com/api/health
```

## 6. Deploy the frontend (on your Mac)

Files added to `~/Dropbox/website`:

- `watchlist-enricher.html`
- `what-should-we-watch.html`
- `css/letterboxd-tools.css`
- `js/letterboxd-tools.js`
- a Letterboxd section in `tools.html`

```bash
cd ~/Dropbox/website
npx wrangler deploy          # or however the site normally ships
```

Then open `https://peterwdunphy.com/watchlist-enricher.html`, enter
`filmwhore`, and run your own username.

## Operating notes

- **Logs:** `journalctl -u boxd-api -f`
- **Restart after a code change:** re-run the rsync from step 1, then
  `systemctl restart boxd-api`
- **Cache:** `/root/apps/letterboxd-tools/backend/data/cache.sqlite`, shared by
  every user and safe to delete (it rebuilds). It grows a few MB per hundred
  films.
- **Timeouts:** Cloudflare Tunnel allows long responses, and the API returns a
  job id immediately and polls, so a slow scrape never sits on one request.
- **The beta password** is checked server-side on every call, but it is a shared
  secret anyone admitted can pass along. It keeps the tools quiet; it is not
  real security, so keep anything sensitive out.
- Both pages are `noindex` and are deliberately NOT in the site header nav yet,
  only linked from `tools.html`. Add them to the nav in every page when the beta
  opens up.

## Local development

```bash
cd backend && ../.venv/bin/uvicorn api:app --reload --port 8011
cd ~/Dropbox/website && python3 -m http.server 8000
# http://localhost:8000/watchlist-enricher.html
```

`js/letterboxd-tools.js` targets `localhost:8011` automatically when the page is
served from localhost, so no edits are needed to switch environments.
