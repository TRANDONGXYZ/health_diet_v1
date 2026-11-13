# Render Free public beta

Repository: https://github.com/TRANDONGXYZ/health_diet_v1 · branch: **main**.

## Short deployment checklist

1. Create a **Supabase Free** project. Enable **Authentication → Sign In / Providers → Anonymous Sign-Ins**. Run [supabase/beta.sql](supabase/beta.sql), then [supabase/offline.sql](supabase/offline.sql), in its SQL Editor. This creates private per-user records, a private `health-diet-images` bucket, access policies, and durable AI usage counters. Keep the Data API enabled for `public`. Do not make the bucket public.
2. Push these changes to **main** in the existing GitHub repository. No personal data, `.env`, or `config.local.json` should be committed. The Docker build explicitly excludes them.
3. In Render, choose **New → Web Service → Git Provider**, connect GitHub, and authorize access to `TRANDONGXYZ/health_diet_v1`. Select this repository from the connected account. **Do not use “Public Git Repository” URL deployment**: automatic deployment requires the connected Git provider.
4. Use the exact settings below, enter the environment variables, and deploy. The included `render.yaml` is an alternative Blueprint configuration; importing it still requires linking GitHub and supplying the unset values.
5. Confirm the service's actual `https://…onrender.com` address. Set `PUBLIC_ORIGIN` to that exact origin with no trailing slash/path. If Render assigned a suffix to the name, update this value and redeploy. Keep `SESSION_SECRET` unchanged across deploys. Open `/healthz`, then the app.
6. In the service's **Settings → Build & Deploy**, choose **Auto-Deploy → On Commit**, and verify **Branch → main**. Push a small change to `main` and confirm a new deploy appears. Once deployed, the app runs independently of the laptop.

Render settings:

| Setting | Value |
| --- | --- |
| Service type | Web Service |
| Name | `health-diet-beta` (Render may append a suffix) |
| Repository | `TRANDONGXYZ/health_diet_v1`, connected via GitHub |
| Branch | `main` |
| Root Directory | Leave blank: `app.py` and `Dockerfile` are at repository root |
| Language / Runtime | **Docker** |
| Instance Type | **Free**, one instance |
| Region | Singapore; place Supabase nearby if practical |
| Dockerfile Path | `./Dockerfile` |
| Docker Build Context | `.` |
| Build Command | Leave blank; Render builds the Dockerfile |
| Docker Command / Start Command | Leave blank; image CMD is `python app.py` |
| Health Check Path | `/healthz` |
| Auto-Deploy | **On Commit** |

The image uses Python 3.12, installs `requirements.txt` and Debian `ffmpeg`/`ffprobe`, and runs as a non-root user. There is no frontend build: the Python server serves `public/` directly. Hosted startup binds to **`0.0.0.0`**, using Render's **`PORT`** automatically (default 10000). Do not run `setup.cmd` on Render: it downloads Windows/local Whisper assets which hosted mode does not need.

## Environment variables

Enter these under Render **Environment**. Never enter the Groq key in the website or commit it to GitHub.

| Variable | Value and where to find it |
| --- | --- |
| `APP_MODE` | `hosted`; already set in the image and Blueprint |
| `GROQ_API_KEY` | Create an operator key at https://console.groq.com/keys. Only the backend uses it. If absent/invalid, AI fails clearly while manual features continue to work. |
| `SUPABASE_URL` | Project URL from the Supabase project's **Connect** dialog or **Project Settings → Data API**, such as `https://PROJECT_REF.supabase.co` |
| `SUPABASE_PUBLISHABLE_KEY` | Project **Settings → API Keys → Publishable key** (`sb_publishable_…`). The legacy `anon` key is also accepted. **Never use a secret or service-role key.** |
| `SESSION_SECRET` | A stable random secret, at least 32 characters. The Blueprint generates it. For manual setup, run `python -c "import secrets; print(secrets.token_urlsafe(48))"` on your own computer and paste the result into Render. Keep it stable to preserve anonymous sessions. |
| `PUBLIC_ORIGIN` | The actual HTTPS Render URL, for example `https://health-diet-beta.onrender.com`, without trailing slash or path. If you later move to a custom domain, use that domain and expect a new browser cookie identity there. |
| `PORT` | Supplied by Render; do not set it manually |
| `USDA_API_KEY` | Optional operator-owned USDA key from https://fdc.nal.usda.gov/api-key-signup; otherwise the existing `DEMO_KEY` is used |

No Supabase admin key, database password, GitHub token, or tester-provided API key is needed at runtime. Render's GitHub integration manages repository access. `AI_PROVIDER=groq` is an optional developer switch to exercise Groq locally without enabling hosted storage; normal `start.cmd` continues to use Ollama and local SQLite.

## Storage and anonymous testers

The browser now uses IndexedDB for local-first saves, including photos and an ordered sync queue. Manual actions work immediately without a server response, and the app shell is cached for offline reopens. See [OFFLINE.md](OFFLINE.md) for the upgrade, flow audit, protocol and measurements. Local development syncs to `data/meals.sqlite3`; hosted development syncs to Supabase.

Hosted mode stores every tester's tables (recipes, foods, weekly plans, settings, caches and week collapse choices) in a separate Supabase `health_beta_data` row. Uploaded images use private Supabase Storage paths `USER_ID/SHA256.png`. Anonymous Supabase users receive unique user IDs without a registration screen. Row and storage policies require `auth.uid()` to match the owner. The backend uses each tester's own access token for all database/storage requests, so it does not bypass these policies.

A signed **Secure, HttpOnly, SameSite=Lax** cookie retains the anonymous identity and refresh token across server restarts. Tokens/keys are not placed in HTML or JavaScript. Each tester receives a separate CSRF token; job status, progress, cancellation, media and backups are also scoped to that tester. Cookie refresh preserves the same identity. Clearing cookies, changing browsers/devices or changing the origin loses anonymous access; without registration there is no account recovery. Tell testers to keep cookies and export backups.

The browser acknowledges a save after committing the local edit and its outbox entry in one IndexedDB transaction. Sync sends only affected records/slots with operation IDs and base versions. Supabase commits and durable receipts remove queued work only after acknowledgement; conflicts preserve unsynced local work for explicit review. Private server SQLite/photo files are caches for legacy routes and online features. Ordinary sync uses PostgreSQL delta RPCs, with no full-workspace HTTP transfer. Application files are cached together by a versioned service worker; personal API responses are not part of that cache.

For an existing installation, run `supabase/offline.sql` before deployment. It adds owner-scoped record versions, tombstones, receipts and an upgrade trigger that also observes older app writes. No environment variables change. First use migrates the existing tester workspace and images into the browser; later refreshes request only changes since the saved cursor.

ZIP export/import retains the existing versioned `backup-v1.json` plus `images/` format, including all weeks, deleted slots, collapse states and tester settings. Import confirmation and validation still apply; import replaces only that tester's workspace. Operator environment secrets are not exported. Local backups can be imported into the tester's own hosted session. Hosted model/provider settings are managed by the operator; imported local model names/keys do not change hosted inference. Hosted bounds: **12 MB structured records, 32 MB photos per tester; 48 MB ZIP upload/expanded validation**, with the existing 500 × 500 PNG and 4 MB individual photo validation. Local backup limits stay unchanged.

## Hosted AI and beta limits

- Text extraction, weekly suggestions, image recognition and source-backed nutrition reasoning use **`qwen/qwen3.8-27b`**. Recipe detection/extraction keeps the existing Vietnamese/English prompts and evidence checks, and never translates the source back and forth. Interface and planning explanations remain English.
- Audio uses Groq's **`whisper-large-v3-turbo` transcription endpoint**, not its translation endpoint; language is auto-detected. ffmpeg converts audio to mono 16 kHz FLAC. Uploaded audio/video is limited to **24 MB and ten minutes**. Temporary media is deleted after use.
- Public YouTube/Facebook imports still try captions, then retrieve audio if available. Hosting-provider IPs may be blocked by video sites. The same source editor accepts pasted text and an uploaded `.txt`, supported audio, or video file the tester is allowed to use. Unsupported sites, blocked/private links, missing tools and over-limit media provide this fallback. No browser cookies, sign-in credentials or download bypasses are used.
- The SQL limiter reserves budget **before** each Groq request: **20 units/tester/day, 200 units across the entire beta/day**, plus **4 requests/tester/minute and 10 globally/minute**. A text/image call costs one unit; an audio transcription costs five. Counters persist across redeploys; daily windows reset at UTC midnight. A failed request still consumes its reservation. To change caps, edit the limiter in `supabase/beta.sql` and rerun it.
- Actual Groq account/model quotas can be lower. HTTP 429 shows a quota message; other provider failures preserve source text and existing plans. No silent fallback to a different model or the operator's laptop is used. Manual editing, calorie arithmetic, Simple Rotation, Randomize and backup remain available when AI is unavailable, even if Supabase or Render is unavailable, once this browser has loaded its data and application files.
- Inference is limited to one active provider call per server process. There is an additional new-session throttle (20/hour per connecting address) and a maximum of 100 in-memory session records per process. Supabase also limits anonymous sign-ins by source IP, so a small public beta behind one Render egress IP shares that signup allowance. These are basic budget controls, not full bot protection. Keep the beta small and monitor Groq/Supabase usage; registration and CAPTCHA flows are outside this version.

## Verification and handoff

The current offline-first upgrade and new verification commands are documented in [OFFLINE.md](OFFLINE.md). Install `supabase/offline.sql` before deploying this version. The older latency measurements below describe the acknowledgement-first app before this refactor.

For the weekly-delete latency fix, rerun the updated `supabase/beta.sql` to add
`health_beta_delete_week`, then deploy the updated backend and frontend together.
See [PERFORMANCE.md](PERFORMANCE.md) for measured causes, timing commands,
`Server-Timing`/Render Logs, and warm versus idle tests. Existing databases work
during rollout through a slower compatibility path until the new function exists.

If deployment succeeds but the app cannot load, check **Supabase Authentication → Sign In / Providers → Anonymous Sign-Ins** first: enable it and save. Merely creating the SQL tables does not enable anonymous authentication. On updated builds, startup errors identify disabled sign-ins, a mismatched project key, a missing table/function, access-policy failures, or rate limits. Render Logs include only the operation, HTTP status and recognized provider error code, never API keys, cookies or raw provider responses. If a SQL object is missing, rerun the complete updated `supabase/beta.sql` in the same project as `SUPABASE_URL`. Preserve `SESSION_SECRET` and existing browser cookies.

Offline checks (no real credentials, no personal data):

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
.\.venv\Scripts\python.exe tests/hosted_restart.py
node tests/browser.cjs
node tests/weekly_browser.cjs
node tests/hosted_ui_browser.cjs
```

The hosted unit tests exercise real HTTP dispatch with explicit Supabase/Groq doubles: separate identities/CSRF, inaccessible other-user images/jobs, durable records, images, collapse choices, ZIP export/import and rejected imports, async job persistence, no acknowledged save on storage failure, and working rotation/randomization after AI limits. The restart test launches the actual hosted app entrypoint twice, verifies `0.0.0.0`/`PORT`, removes process-local session caches, and recovers the same tester's data from the persistent provider double. This does **not** certify live Supabase RLS or real model behavior.

The optional SQL regression check uses an actual in-memory PostgreSQL engine (PGlite) with fixture Supabase schemas. It reproduces the original `42601` error, compiles and reruns the corrected migration, and executes the revision checks, owner policies and AI limits:

```powershell
npm install --prefix .tools/sql-check --no-save --package-lock=false --ignore-scripts @electric-sql/pglite
node tests/beta_sql.cjs
```

If you copied an earlier `beta.sql` that failed at the quota function's `CASE` expression, replace the entire SQL Editor query with the updated file and run it again. The script can be rerun without deleting tester records.

After configuring actual credentials and deploying, run:

```powershell
# Creates disposable anonymous testers and checks actual Supabase row/storage policies.
# Set SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY in this shell first.
.\.venv\Scripts\python.exe tests/live_beta.py --rls

# Separate browser sessions, photos, recipe/plan persistence and ZIP restore on Render.
.\.venv\Scripts\python.exe tests/live_beta.py --url https://YOUR-SERVICE.onrender.com

# Keep the test's cookies in memory while you restart/redeploy Render, then press Enter.
.\.venv\Scripts\python.exe tests/live_beta.py --url https://YOUR-SERVICE.onrender.com --restart

# Optional real Vietnamese/English extraction and image calls through hosted Groq.
.\.venv\Scripts\python.exe tests/live_beta.py --url https://YOUR-SERVICE.onrender.com --ai
```

Also upload a short permitted recording through the source editor to verify real ffmpeg/Groq audio, test a public video (site availability varies), open the app in two browser profiles, and verify a push to `main` triggers **On Commit** deployment. Live tests create small disposable tester datasets and use quota only with `--ai`; they never access the local user's cookbook. Remove disposable anonymous users from the Supabase dashboard when finished; do not delete active testers.

At implementation time no Groq/Supabase credentials or Render URL were supplied, so live model calls, live RLS, a Render build/deploy and automatic GitHub deployment require the operator's handoff checks. Docker CLI is installed in the development environment but its daemon is unavailable; native hosted startup/restart is tested, and the container must still be built by Render (or a running local Docker engine).

Free-tier constraints: Render Free sleeps after 15 minutes idle and cold-starts on the next request (often about a minute); it is independent of the laptop but not always warm. Its filesystem is temporary. Supabase Free has quotas and may pause after a week of inactivity, requiring dashboard resumption. Neither service is a production availability guarantee. Existing saved data lives in Supabase, and tester ZIP backups remain important.

Provider references: [Render Free](https://render.com/docs/free), [Docker settings](https://render.com/docs/docker), [Auto-Deploy](https://render.com/docs/deploys), [Blueprint syntax](https://render.com/docs/blueprint-spec), [Groq vision](https://console.groq.com/docs/vision), [Groq structured outputs](https://console.groq.com/docs/structured-outputs), [Groq audio](https://console.groq.com/docs/speech-to-text), [Supabase anonymous sessions](https://supabase.com/docs/guides/auth/auth-anonymous), [Storage RLS](https://supabase.com/docs/guides/storage/security/access-control), [Supabase Free limits](https://supabase.com/pricing).
