# Deploying the Face Attendance System

## 1. What you need
* A server/container with **2 vCPU and 2–4 GB RAM** (each web worker loads dlib's models, ~150–250 MB, and face matching is CPU-bound).
* **PostgreSQL** (managed is fine). SQLite is for local development only.
* **HTTPS.** Browsers only allow camera access on `https://` (or `localhost`). Put the app behind a TLS-terminating proxy (nginx, Caddy, or your host's built-in HTTPS).

## 2. Required settings (environment variables)
| Variable | Example | Notes |
|---|---|---|
| `SECRET_KEY` | 64 hex chars | **Must be identical on every worker/instance.** `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL` | `postgresql://user:pass@host:5432/db` | `postgres://` is converted automatically |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | | Used by `init-db`. If the password is empty a random one is printed once. |
| `APP_ENV` | `production` | default |
| `TRUSTED_PROXY_COUNT` | `1` | proxies in front of the app; `0` if directly exposed |

The app **refuses to start** in production without `SECRET_KEY` and `DATABASE_URL`, instead of silently misbehaving. See `.env.example` for every option.

## 3. First deployment
```bash
pip install -r requirements.txt          # or: docker build -t face-attendance .
python run.py init-db                    # tables, indexes, admin account (safe to re-run on every deploy)
gunicorn -c gunicorn.conf.py wsgi:app    # production server
```
Docker: `docker run -p 8000:8000 --env-file .env face-attendance` (the image runs `init-db` then gunicorn).
Heroku/Render-style platforms: the included `Procfile` has `release:` and `web:` entries.

> **dlib builds from source** (5–15 min, needs cmake + a C++ compiler). Build the Docker image in CI or on a machine with enough RAM and push it, rather than building on a tiny host.

### nginx (if you run your own proxy)
```nginx
server {
    listen 443 ssl http2;
    server_name attendance.yourschool.edu;
    client_max_body_size 6m;                 # enrollment photos
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

## 4. Capacity guide
* Face work is limited **per worker** by `FACE_MAX_CONCURRENT` (default 2). Extra scans wait up to 8 s, then the camera page shows "server busy – retrying" instead of overloading the server.
* Rule of thumb: **one worker per CPU core**, 4 threads each. Scale by adding cores/instances, not threads.
* DB connections used ≈ `WEB_CONCURRENCY × (DB_POOL_SIZE + DB_MAX_OVERFLOW)`. Keep it under your database's connection limit.
* With several workers/instances set `RATELIMIT_STORAGE_URI=redis://...` so login limits are shared; face data and rosters are already kept in sync through the database (`app_state` table).
* Campus Wi-Fi puts many users behind one public IP, so IP limits are generous; the strict login limit is per account (`LOGIN_ACCOUNT_RATE_LIMIT`).

## 5. Operating it
* **Health checks:** `GET /healthz` (process up) and `GET /readyz` (database reachable).
* **Logs:** written to stdout. Every sensitive action is logged as `AUDIT ...` and stored in the `audit_log` table (manual attendance changes, deletions, role changes, enrollments).
* **Backups:** back up PostgreSQL daily and test a restore. The numeric face templates live in the `students` table.
* **Updating:** deploy, then `python run.py init-db` (adds new tables/indexes; it does not alter existing columns — adopt Flask-Migrate before changing columns).
* **Rotating `SECRET_KEY`** logs everyone out (that is all it does).

## 6. Day-one checklist
- [ ] `.env` was **not** committed or shipped. If an old copy was ever shared, rotate `SECRET_KEY`, the database password and the admin password.
- [ ] Change the admin password after first login.
- [ ] Site served over HTTPS; camera works on a teacher's phone/laptop.
- [ ] Run a real pilot class: enroll ~20 students, scan them in the real room/lighting, and check the match distances in the logs (`Scan course=… best_distance=…`). Tune `CONFIDENCE_THRESHOLD` / `MATCH_MARGIN` only with that data.
- [ ] Publish a privacy notice and keep the consent wording (face data is biometric, sensitive personal data under Nigeria's NDPA 2023). Decide a retention period and how a student requests deletion (admin → delete student).
- [ ] Back-ups scheduled.

## 7. Running the tests
```bash
pip install -r requirements-dev.txt
pytest -q
```

## 8. Known limitations (not solved by this code)
* **No liveness detection.** A printed photo or a phone screen shown to the camera can still be recognised. The teacher-run camera and the manual-override audit trail reduce the risk; true liveness needs an extra model.
* Student identity is self-declared at registration (no integration with the university's student registry or e-mail verification).
* Only one face template per student; accuracy is best when the room's lighting resembles enrollment.
* Logout is a GET link and there is no Content-Security-Policy header yet (pages use inline scripts and CDN assets).
