# What changed and why

Every item maps to a problem found when reading the original code. Files marked **(new)** were added.

## Read this first: behaviour changes and the one-time upgrade step
1. **Run `python run.py init-db` once** after deploying. It creates the new `app_state` and `audit_log` tables and the database indexes. It is safe to re-run.
2. **Set `SECRET_KEY` and `DATABASE_URL`.** In production the app now refuses to start without them (previously it silently used random keys / SQLite).
3. **Students can no longer edit their Student ID** on the profile page (it is their identity in attendance records). Admins can still change it.
4. **Enrollment now requires a consent checkbox**, and **raw face photos are no longer stored** unless `STORE_FACE_IMAGES=true` (only the numeric face template is kept).
5. **`/recognize_face` is retired** (returns 410). It let any logged-in account mark attendance for anyone. Attendance is marked only inside a course session by that course's teacher.
6. **A teacher can only delete a student's face data if no other teacher's course uses it**; otherwise an admin must do it.
7. **Password minimum is now 8**; e-mails are case-insensitive (existing accounts keep working).
8. **"Marked This Session"** now shows only the active session (it used to mix in the last 5 sessions, so the counter was wrong).
9. Dependencies changed: `opencv-python` -> `opencv-python-headless`; added `gunicorn`, `Flask-Limiter`, `tzdata`, `Pillow`.

## Would have crashed or broken once hosted
| Problem | Fix |
|---|---|
| `debug=True` on `0.0.0.0` (remote code execution via the debugger) | `run.py` is dev-only, debug only with `FLASK_DEBUG=1` outside production; production uses `gunicorn -c gunicorn.conf.py wsgi:app` |
| Random logouts with >1 worker (`BOOT_ID` and `SECRET_KEY`/CSRF secret were different in every worker process) | `BOOT_ID` removed; `SECRET_KEY` is required and shared; CSRF secret derives from it |
| Cached `Student` ORM objects became expired/detached after the next commit -> `DetachedInstanceError` on the following scans | New `face_cache.py` caches plain records + a numpy matrix, never ORM objects |
| Face/roster caches were per-process: enrolling or deleting a student only affected one worker | Version counters in the `app_state` table; every worker reloads when the version changes |
| Expired login during a lecture -> HTML redirect -> scan page looped "Error processing scan" forever | JSON 401 for scripts, and the scan page now stops and redirects to login |
| Enrollment photo is a large form field; Flask/Werkzeug 3.1+ rejects fields > 500 KB (HTTP 413) | `MAX_FORM_MEMORY_SIZE` raised |
| `postgres://` URLs (Heroku/Render) rejected by SQLAlchemy 2 | Normalised automatically |
| Idle PostgreSQL connections dropped by the host -> random "server closed the connection" | `pool_pre_ping`, `pool_recycle`, pool limits |
| `opencv-python` needs `libGL` on servers | `opencv-python-headless` |
| A 500 on the home page redirected to the home page (redirect loop) | Self-contained 500 page, JSON for scripts |
| CSRF protection was switched off | Enabled; token added to every form and automatically to every `fetch()`; no 1-hour token expiry |

## Attendance logic
* Camera scans: image size capped and downscaled, decompression-bomb guard, max concurrent face jobs per worker (busy -> "retrying", not a crash), request timeout on the page.
* A background person no longer blocks scanning: the clearly largest face is used; two similar-size faces are still rejected; far-away faces get "step closer".
* **Manual mark / remove** buttons on the session page (teacher override) with reason, saved in `remarks` and the audit log. The old message told teachers to "mark manually" but no such feature existed.
* Duplicate scans (race) return "already marked" instead of an error; two active sessions for one course are impossible (database unique index); a session left open overnight closes itself.
* Matching rules (threshold, runner-up margin) are unchanged, now vectorised (about 20x faster at 20,000 faces).

## Accounts, enrollment, privacy
* Rate limits: per-IP (generous, campus NAT) plus strict per-account login limit; registration, enrollment, scans, public invite check.
* Atomic invite/enrollment-token use (two people could redeem the last use), session-fixation protection, case-insensitive e-mail, constant-time login for unknown e-mails.
* Profile page bug: saving a Student ID created a faceless student row that permanently blocked face enrollment. Fixed, and such rows are repaired automatically at enrollment.
* The "duplicate face" error used to show another student's **name, ID, course and e-mail** to whoever triggered it. Now generic (details only in the server log). Same for duplicate ID/e-mail messages.
* Deleting users/students now cleans every foreign key (PostgreSQL would have refused), removes the stored photo, refreshes the cache and writes an audit entry. Accounts that started sessions must be deactivated, not deleted.
* Teachers can only view sessions of their own courses through the API.

## Performance
* Removed per-row database queries on the session page, "Marked This Session", student stats API, search; face-encoding column no longer loaded for lists; search filtered in SQL with a result cap.
* Added indexes; home page counters cached for 60 s; the home "attendance today" figure used an unused table and was always 0, now real.

## Time handling
Timestamps are stored in UTC and shown in `APP_TIMEZONE` (default `Africa/Lagos`); session dates/"today" use that zone, so lectures no longer roll over at the wrong hour on a UTC server.

## New files
`app/face_core.py`, `app/face_cache.py`, `app/timeutil.py`, `app/audit.py`, `app/bootstrap.py`, `app/http_utils.py`, `wsgi.py`, `gunicorn.conf.py`, `Dockerfile`, `Procfile`, `.dockerignore`, `.env.example`, `requirements-dev.txt`, `README_DEPLOY.md`, `tests/`.
Removed from the package: the real `.env`, `venv/`, and the student photo in `uploads/faces/`.

## How much of this was actually verified
Executed here and passing: **50 automated tests** (36 on image handling / face selection / matching incl. a 20,000-face speed check / concurrency gate; 14 on HTTP behaviour, error handlers, headers and config validation), a name-resolution check of every function in all 18 modules (proved by planting a bug), Jinja syntax of every template, and confirmation that all 63 routes and every `url_for()` resolve.

**Not executed** (the sandbox has no network, so SQLAlchemy, PostgreSQL and dlib could not be installed): `tests/test_app_integration.py` (a database-backed test of the full scan, manual-override, cache and account flows), real face recognition with dlib, PostgreSQL itself, and any load test. Please run `pytest -q` on your machine first; any failure there is the most likely place for a remaining issue, and I want to hear about it.

## Not changed (known limits)
No liveness detection; student identity is self-declared; one face template per student; no CSP header; logout is a GET link; admin/teacher dashboards load their lists in full (fine for hundreds of users, add pagination for tens of thousands); `templates/admin_users.html` was already syntactically broken and is unused. Schema changes still use `create_all` (adopt Flask-Migrate before altering existing columns).
