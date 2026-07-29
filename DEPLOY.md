# Deploying the beta

Two halves: the API on the DigitalOcean droplet (`ssh do`), and the static
pages on peterwdunphy.com. The frontend already points at
`https://boxd-api.peterwdunphy.com`, so the DNS + proxy names below matter.

## 1. Backend on the droplet

```bash
# copy the code (excludes .env, cache, venv — those are gitignored)
rsync -av --exclude '.venv' --exclude '__pycache__' --exclude 'data' \
  ~/Library/CloudStorage/Dropbox/misc_research/random/letterboxd_tools/backend/ \
  do:/opt/letterboxd-tools/

# copy your secrets separately
scp ~/Library/CloudStorage/Dropbox/misc_research/random/letterboxd_tools/.env \
  do:/opt/letterboxd-tools/.env

ssh do
cd /opt/letterboxd-tools
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# smoke test before wiring up a service
.venv/bin/python -c "from lbtools import letterboxd as l; print(len(l.get_watchlist('peterdunphy', max_pages=1)), 'films OK')"
```

`config.py` looks for `.env` one directory above `backend/`, so on the droplet
either keep that layout (`/opt/letterboxd-tools/.env` with code in the same
folder needs the tweak below) or export the vars in the systemd unit instead —
the unit below does exactly that, which is simpler.

### systemd service

`/etc/systemd/system/boxd-api.service`:

```ini
[Unit]
Description=Letterboxd Tools API
After=network.target

[Service]
WorkingDirectory=/opt/letterboxd-tools
EnvironmentFile=/opt/letterboxd-tools/.env
Environment=BETA_PASSWORD=filmwhore
ExecStart=/opt/letterboxd-tools/.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8010
Restart=always
User=root

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now boxd-api
systemctl status boxd-api
curl localhost:8010/api/health
```

### nginx + TLS

Mirror whatever `amc-api.peterwdunphy.com` already uses. New site file
`/etc/nginx/sites-available/boxd-api`:

```nginx
server {
    server_name boxd-api.peterwdunphy.com;
    client_max_body_size 25M;          # Letterboxd export ZIPs
    location / {
        proxy_pass http://127.0.0.1:8010;
        proxy_set_header Host $host;
        proxy_read_timeout 600s;       # big profiles take minutes
    }
}
```

```bash
ln -s /etc/nginx/sites-available/boxd-api /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d boxd-api.peterwdunphy.com
```

Add an A record for `boxd-api` pointing at the droplet before running certbot.

## 2. Frontend

Files added to `~/Dropbox/website`:

- `watchlist-enricher.html`
- `what-should-we-watch.html`
- `css/letterboxd-tools.css`
- `js/letterboxd-tools.js`
- a Letterboxd section on `tools.html`

Deploy the way the site normally deploys (`npx wrangler deploy`, or the
Git-connected build).

Both pages are `noindex` and password-gated, and they are deliberately NOT in
the header nav dropdown yet — only linked from `tools.html`. Add them to the
nav in every page when the beta opens up.

## 3. Local development

```bash
cd backend && ../.venv/bin/uvicorn api:app --reload --port 8011
cd ~/Dropbox/website && python3 -m http.server 8000
# open http://localhost:8000/watchlist-enricher.html
```

`js/letterboxd-tools.js` auto-targets `localhost:8011` when served from
localhost, so local pages hit the local API with no edits.

## Notes

- The beta password (`filmwhore`) is checked server-side on every request, but
  it is a shared secret visible to anyone who gets in. It keeps the tool
  quiet; it is not real security. Nothing sensitive sits behind it.
- The SQLite cache at `backend/data/cache.sqlite` is shared across all users
  and makes repeat runs much faster. It is safe to delete; it rebuilds.
- Scraping is throttled to ~0.6s/page. A 900-film profile takes about 40s the
  first time and seconds afterward.
