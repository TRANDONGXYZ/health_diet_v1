# Offline-first storage and rollout

The UI commits manual changes to IndexedDB before reporting success. Server
responses are no longer the save boundary. No new packages, credentials, service,
frontend build or Render settings are needed.

## Upgrade the existing hosted beta

1. Export a backup from the existing app.
2. In the same Supabase project's SQL Editor, run the **complete
   [supabase/offline.sql](supabase/offline.sql)**. `beta.sql` must already be
   installed. For a new project run `beta.sql`, then `offline.sql`. This upgrade
   is repeatable and preserves existing data. It can run before deploying the
   new app: a trigger also tracks writes from the older deployment.
3. Push the updated app to GitHub `main` and let the existing Singapore Render
   service auto-deploy. Keep `SESSION_SECRET`, Supabase variables and
   `PUBLIC_ORIGIN=https://health-diet.onrender.com` unchanged.
4. Open the app online once. Existing anonymous-session recipes, food values,
   every saved week, collapse choices, settings and private photos are copied
   into this browser. Wait for **Online · All changes synced** so image downloads
   finish too. Each browser profile/origin has its own offline copy.
5. Reopen the same address offline. The service worker serves the application
   files from cache, and IndexedDB supplies the data. Manual work does not need
   the laptop/server to be running. AI, video retrieval/transcription, online
   nutrition lookups, initial migration and server sync still need a reachable
   backend. Local Ollama continues to work through the local Python server.

The operator applied the SQL upgrade and confirmed it succeeded. App commit
`797085b` was deployed to https://health-diet.onrender.com and verified live on
November 13, 2026. The live checks below used isolated anonymous workspaces and
made no AI calls; Groq inference itself was not exercised by these checks.

## Mutation audit

| Action | Local commit and eventual sync |
| --- | --- |
| Add Week | Week metadata and seven empty slots, one ordered operation |
| Add/Replace meal, including history | One dated slot; other days stay unchanged |
| Delete week | Week tombstone; removes its slots in the same server transaction |
| Save/edit recipe, ingredients, running label | Only that recipe; no nutrition/AI request blocks the local save |
| Delete recipe | Recipe tombstone and “Dish deleted” markers across all affected weeks, locally and on the server |
| Edit food calories/aliases | Only that food reference; calorie arithmetic uses local values |
| Upload/remove dish photo | Browser crop to 500 × 500 PNG; Blob and pending upload saved together; recipe stores its content-derived URL |
| Settings | Settings record; provider secrets remain server-side in hosted mode |
| Collapse/expand | One boolean for that week; current week always renders expanded |
| Simple Rotation/Randomize | Runs in the browser; only generated slots and week metadata are queued; elapsed current-week dates stay unchanged |
| Generate with AI | Sends up to 12 compact saved-dish candidates and target preferences; server returns suggestions; the browser commits the result to its outbox |
| Recipe extraction/photo nutrition | Sends the source or photo needed for that operation; returned food references are saved locally; a failed lookup leaves an explicitly incomplete estimate |
| Export Backup | Builds ZIP v1 from the current local view and photo Blobs, including edits still pending sync |
| Import Backup | Confirmation, complete validation, then one local transaction installs the replacement and queues an explicit restore; image uploads are staged before the atomic server restore |

Unknown ingredients do not prevent a manual save. Background lookup attempts run
when online; unknown weights/values stay visibly incomplete and can be entered
manually. Draft fields remain drafts until Save, and Cancel keeps its existing
meaning.

## Sync protocol and conflicts

IndexedDB database `health-diet-offline-v1` has `records`, `outbox`, `photos` and
`meta` stores. One transaction stores each edit and its outbox operation. Views
replay pending operations over acknowledged records. An acknowledgement and
outbox removal also happen in one transaction. A failed local transaction does
not report a successful save. Browser locks serialize concurrent tabs; edits use
a different lock from the network sync loop, so a slow request cannot block a
local commit.

`GET /api/sync/bootstrap` is the initial full transfer. Background sync runs
only when saved changes are pending. It uses `GET /api/sync/pull?since=CURSOR`
to reconcile changed records and tombstones before uploading the ordered outbox.
An empty queue makes no sync requests: no recurring polling, window-focus sync,
or sync for scrolling or switching app tabs. Failed uploads stay queued for
backoff retries; reconnecting or reopening resumes pending work. Saving an
unchanged record creates no upload. Remote edits from another device are
reconciled on the next pending upload rather than polled while idle. The application shell's cache version is computed from its files;
new workers install a matching complete shell. Private API responses, JWTs and
cookies are never stored in the public shell cache.

`POST /api/sync/push` sends an operation UUID, the last observed cursor, affected
record keys/values and their base versions. Changes apply in one PostgreSQL
transaction with a durable operation receipt. A lost acknowledgement retries
the identical payload/UUID. Local dependency IDs also rebase later queued edits
onto their acknowledged predecessors, including recipe-deletion cascades. New recipes and photos sync before dependent meals.
The local server implements the same protocol and receipts in SQLite.

Server updates patch the existing owner-scoped `health_beta_data` document
inside PostgreSQL. There is no full-workspace HTTP upload/download for ordinary
mutations. `health_beta_sync_records` stores current record versions and deletion
tombstones; `health_beta_sync_ops` retains receipts. The database still processes
the workspace JSON internally; this is a beta-scale solution, not a normalized
database redesign. Old server routes remain for compatibility and their writes
are tracked by the migration trigger.

Conflicting operations stay in the outbox and remain visible locally. **Review
sync conflict** lets the user explicitly keep their edit or use the server's
version. Keeping an edited recipe that was deleted elsewhere saves a new dish
ID, preserving the old dish's deletion markers. A meal cannot recreate a deleted
week or reference a deleted dish. Independent dates have independent versions.
The original record shown in an open editor is checked so a remote change is
not silently overwritten by an old form. Acknowledging an unchanged local
predecessor does not create an artificial conflict.

The signed anonymous cookie continues to identify the tester. Requests use that
tester's JWT and owner policies. If the server identity changes, the client
blocks uploading the previous workspace; its local data stays available for
backup. Browser storage is device/origin-specific: export before clearing site
data or moving domains. Use the UI's backup action for the latest pending edits;
copying the server's SQLite file captures only what has reached that server.

## Verification

Run with Python and Node/Playwright available (Microsoft Edge is used):

```powershell
python -m unittest discover -s tests -p "test_*.py"
node tests/offline_browser.cjs
node tests/browser.cjs
node tests/create_actions_browser.cjs
node tests/week_delete_browser.cjs
node tests/future_backup_browser.cjs
node tests/offline_sql.cjs
node tests/offline_hosted.cjs
```

The PostgreSQL checks use the existing optional
`.tools/sql-check/node_modules/@electric-sql/pglite` test dependency. The browser
checks use temporary data directories and anonymous test workspaces. They never
read a real cookbook, use live Groq quota or need production keys.

Measured in the actual Edge UI on this Windows laptop, with every sync response
held for **3 seconds**:

| Action to visible local completion | Delayed server response | Fully offline |
| --- | ---: | ---: |
| Add Week via Enter | 120 ms | 58 ms |
| Save Recipe | 180 ms | 155 ms |
| Add meal from picker | 98 ms | 152 ms |

Live verification on the Singapore Render service, with sync responses also
held for **3 seconds**:

| Action to visible local completion | Live, delayed sync response | Live, fully offline |
| --- | ---: | ---: |
| Add Week via Enter | 123 ms | 114 ms |
| Save Recipe | 162 ms | 99 ms |
| Add meal from picker | 91 ms | 93 ms |

`tests/live_offline.cjs --url https://health-diet.onrender.com` passed offline
reload with pending changes and a 500px photo, ZIP export/restore while offline,
reconnection and queue drain. A fresh browser context with the same anonymous
cookie bootstrapped the saved recipes, weeks and private image from durable
storage. A different anonymous tester saw no recipes/weeks and could not access
the image. This used no AI quota. The live slot request was **430 bytes**, and its
response was **804 bytes**, containing only that slot and its snapshot/version.
Exact test records and measurements are in ignored
`test-results/live-offline-beta.json`; no cookies or credentials are logged there.

These are local fixture measurements, not live Render measurements. The test
also reloads offline, reconnects, verifies persistence, deliberately loses a
successful response and confirms the retry uses the same UUID with one server
recipe. It covers photos, collapse state, pending ZIP export, Python ZIP
validation, compressed legacy backups, rejected/cancelled imports, calorie
edits, deletion markers and elapsed-day preservation. The hosted test runs the
real HTTP entrypoint against actual PostgreSQL RPCs with fixture authentication
and private image storage; it checks isolation and persistence/receipts across
an app-process restart. The existing general browser regression covers recipe
CRUD, filters, calorie editing, nutrition/camera UI, native-language imports and
AI error/progress handling. A deliberately aborted IndexedDB transaction retains
the old records and form. Planning checks cover resuming/cancelling a task after
reload and rejecting an AI result if that week changed while it was running.

One measured meal-slot request was **430 bytes** of JSON:

```json
{
  "id": "operation-uuid",
  "kind": "change",
  "since": 3,
  "changes": [{
    "key": "meals/2026-11-16/2026-11-16",
    "base": 2,
    "value": {
      "date": "2026-11-16",
      "recipe_id": "saved-recipe-uuid",
      "timing": "morning",
      "prep": "Review the original source and weigh the listed ingredients ahead of time.",
      "reason": "Selected from your saved dishes.",
      "calories": {"total": 0, "per_serving": null, "complete": false, "missing": []}
    }
  }]
}
```

Its **697-byte** response contained the acknowledgement UUID, cursor, and that
one slot's new value/version, including its historical recipe snapshot. There
were no other weeks, food library, settings, recipe collection or image bytes.
Sizes vary with that dish's details. If other records changed since the supplied
cursor, the acknowledgement includes those deltas too. Changes such as clearing
a week's vegan-only flag after a non-vegan replacement can legitimately return
that affected metadata record as well. Measurements and the exact captured
request/response are written to ignored `test-results/offline-performance.json`.

After deployment, repeat the same checks in a normal browser profile using
DevTools Network: throttle a sync request or select Offline, use Enter in Add
Week and Save Recipe/Add, reload offline, then reconnect. Expect local UI
completion before the pending sync response, followed by **All changes synced**.
Filter requests by `/api/sync/`; ordinary actions must not call `/api/state` or
transfer a `tables` workspace. A slot edit has one `meals/WEEK/DATE` key, and no
`image` bytes. Preserve a pending operation's UUID when testing a lost response.
