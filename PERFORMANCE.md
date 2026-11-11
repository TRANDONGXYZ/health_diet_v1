# Weekly plan deletion timings

The browser timing check uses a new isolated anonymous tester and creates its own
empty future weeks. It never opens an existing tester's cookbook. `--images`
also creates seven disposable photo recipes and an additional populated week.
No AI calls are needed. Test timing JSON goes under ignored `test-results/`.

## Confirmed cause

Before the change, `deleteWeek()` waited for `POST /api/plan/delete` and then
`GET /api/state` before removing the week from the screen. Hosted middleware
hydrated every table and photo manifest before each request, including image
requests, under one tester-wide lock. The delete POST then serialized and saved
the entire document. One delete therefore required three database round trips
(read, full save, another read), and could queue behind unrelated image reads.

Browser measurements on the live Render service showed 3.325/3.446 seconds for
two warm empty-week deletes. With seven photos loading, the second delete took
5.779 seconds: 4.721 seconds in the POST and 1.050 seconds in the state reload.
The existing checkout and deployed script had no weekly-delete confirmation.

An instrumented hosted fixture with a fixed 350 ms storage delay confirmed the
server breakdown. The image-load scenario's second delete spent 1.239 seconds
waiting for the workspace lock, 0.363 seconds hydrating data, 0.016 seconds doing
the delete, and 0.357 seconds persisting. The subsequent state fetch added 0.497
seconds. The complete click-to-removal time was 2.502 seconds.

## Change

- Confirmation opens synchronously and includes the selected week range.
- Supabase `health_beta_delete_week` atomically removes only the specified plan
  from the authenticated tester's document and increments its revision. The
  existing revision check rejects stale full saves that could resurrect it.
- The delete endpoint uses that one RPC without acquiring the photo/workspace
  hydration lock, loading unrelated tables, downloading images or saving a
  replacement document. Active AI generation for the same week still blocks it.
- After durable acknowledgement, the browser removes only that week from its
  existing state and rerenders. It does not issue a full state fetch. Earlier
  in-flight refreshes are invalidated; cancel and error paths retain the week.
- If the new SQL function has not been installed yet, deletion falls back to the
  previous durable storage path. Rerun the complete updated `supabase/beta.sql`
  to enable the performance improvement.

In the same 350 ms storage fixture, confirmation opened in 0–1 ms and confirmed
deletion completed in 0.361–0.363 seconds, including while photos loaded. The
timing header reports one `week_delete` call and zero load/persist time. Local
confirmed deletes took 0.030–0.052 seconds (confirmation opened in 1–3 ms).
These fixture/local numbers are separate from live deployment measurements.

## Verified live deployment

Commit `e557eee` was pushed to `main` and automatically deployed to
https://health-diet-v1.onrender.com. The updated SQL function was installed in
Supabase before testing. The same isolated browser check was repeated after the
new frontend and backend were live:

| Scenario | Before: click to disappearance | After: confirmation to disappearance |
| --- | --- | --- |
| Warm Render, attempt 1 | 3.325 s | 1.174 s |
| Warm Render, attempt 2 | 3.446 s | 1.163 s |
| Seven photos loading, attempt 1 | 2.554 s | 1.136 s |
| Seven photos loading, attempt 2 | 5.779 s | 0.693 s |
| Local, attempt 1 | 0.103 s | 0.052 s |
| Local, attempt 2 | 0.068 s | 0.030 s |

The previous build did not show a week-delete confirmation; it has no comparable
pre-fix dialog-opening measurement. The updated live confirmation opened in
0–1 ms. Human decision time is excluded. After-fix complete click-to-removal
times were 1.183/1.170 s (warm) and 1.148/0.699 s (photos).

Live response headers confirm the new RPC path: `lock_wait`, `load` and `persist`
were all zero; `week_delete` was the only storage operation. The two warm RPCs
took 879/854 ms on the server, with the remaining browser time in transit. No
follow-up `/api/state` request occurred. Both deleted weeks stayed absent after
reload, while the unrelated populated week remained.

Before deployment, a separate browser retained its page and anonymous session
without any test refresh traffic for 960 seconds. Its first delete took 3.449 s,
and its second took 3.028 s. This run did not show a distinct cold-start penalty;
other traffic can keep the service awake. It confirms the delay also occurs
while the service is running. The idle experiment was not repeated after the fix.

Raw timing files are local ignored artifacts under `test-results/`:
`delete-live-before.json`, `delete-live-after.json`,
`delete-live-images-before.json`, `delete-live-images-after.json`,
`delete-live-idle-before.json`, `delete-local-before.json`,
`delete-local-after.json`, `delete-hosted-before.json`, and
`delete-hosted-after.json`. They contain timings, not credentials or tester data.

## Reproduce and capture

These developer scripts require Node, Playwright and installed Microsoft Edge:

```powershell
node tests/measure_week_delete.cjs --local --output test-results/delete-local.json
node tests/measure_week_delete.cjs --hosted-sim --images --output test-results/delete-hosted.json
node tests/measure_week_delete.cjs --url https://health-diet-v1.onrender.com --output test-results/delete-live.json
node tests/measure_week_delete.cjs --url https://health-diet-v1.onrender.com --images --output test-results/delete-live-images.json
node tests/measure_week_delete.cjs --url https://health-diet-v1.onrender.com --idle-seconds 960 --output test-results/delete-live-idle.json
```

The idle check disables its test page's automatic refresh and sends no test
traffic for sixteen minutes. The first click then uses the retained DOM and
session; the second click measures the already-running service. Other testers
or tabs can still keep Render awake, so sixteen minutes of test inactivity alone
does not prove the service spun down. Render Free's idle spin-down threshold is
[fifteen minutes](https://render.com/docs/free).

Each output separates click-to-confirm, confirmation-return-to-removal, each HTTP
request duration/TTFB, and `Server-Timing`. The test also reloads the page and
verifies both deleted weeks remain absent. The original script's lack of a
confirmation is recorded as `no_confirmation` rather than reported as zero.

Without developer tooling, open browser DevTools → Network, enable **Preserve
log**, clear the list, click Delete and confirm. Filter by `/api/plan/delete`,
inspect **Timing**, and copy its **Server-Timing** response header. No
`/api/state` request should be required for the successful deletion. Check the
week remains deleted after reload. Measure a second delete in the same session.
Clear the list after focus-related state refreshes finish so they are not
mistaken for the delete operation.

Render Logs include `Week delete timing` with safe numeric fields only. The
`week_delete` metric measures the RPC, `lock_wait` detects queueing, and `total`
is elapsed server time. Individual phases can overlap with nested database
metrics: do not add all fields together. No keys, cookies, IDs, recipes, image
contents or provider URLs are logged.

Regression checks:

```powershell
python -m unittest discover -s tests -p 'test_*.py'
python tests/hosted_restart.py
node tests/beta_sql.cjs
node tests/week_delete_browser.cjs
node tests/weekly_browser.cjs
```
