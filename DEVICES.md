# QR pairing and cross-device sync

This shares one workspace between independently authenticated browsers. It does
not copy cookies, session tokens or API keys to another device. Local manual
saves still finish at the IndexedDB commit; pairing and sync need the hosted
HTTPS service. Local Ollama/development mode is unchanged.

## Deploy to the existing Render service

1. Export a ZIP backup before the upgrade.
2. In the **existing Supabase project's SQL Editor**, run the complete
   [supabase/devices.sql](supabase/devices.sql). Existing `beta.sql` and
   `offline.sql` are prerequisites. For a new project run them in this order:
   **beta.sql → offline.sql → devices.sql**. Run `devices.sql` last again if
   reinstalling either prerequisite, since it upgrades their access policies.
   The migration is transactional and rerunnable, preserves existing documents,
   and never reactivates a disconnected browser.
3. Deploy this checkout from GitHub **main** to the existing Singapore Render
   service. The Docker build installs `qrcode` and `websocket-client`. No frontend
   build, new external service, public bucket or service-role key is needed.
4. In **Render → Environment**, add **`DEVICE_PAIRING_ENABLED=1`**, then save and
   redeploy. This setting is deliberately opt-in so code can deploy safely
   before the database upgrade. Keep `SESSION_SECRET`, `PUBLIC_ORIGIN` and the
   existing Supabase variables unchanged. The actual origin remains
   `https://health-diet.onrender.com`.
5. Supabase Realtime must be enabled for **`health_beta_events`** and
   **`health_beta_signals`** in the `supabase_realtime` publication. The migration
   adds them to the standard Supabase publication. You can check with:

   ```sql
   select schemaname, tablename from pg_publication_tables
   where pubname = 'supabase_realtime'
     and tablename in ('health_beta_events', 'health_beta_signals');
   ```

   Expect two rows. The event tables have RLS. Do not disable RLS, expose the
   image bucket, or publish the full document/recipe tables for live sync.
6. Open the app online in the existing browser, retaining its cookies. In
   **Settings → Connected devices → Connect a device**, scan with the iPhone
   camera. Open the link in the browser you intend to keep using, compare the
   codes, approve on the first device, then confirm **Join shared data** on the
   joining browser. Private-mode storage may be discarded by the browser.

If the joining browser has data, use its **Export Backup** action before joining.
The QR expires after five minutes and is single-use. Closing/canceling before
replacement preserves the old dataset and outbox. Every required image is
downloaded, checksum/dimension validated, and staged before one atomic local
replacement. Interrupted activation is recovered on reopen/reconnect. Once the
local replacement succeeds, a lost final acknowledgement is retried rather than
rolling back ready data.

Keep at least one connected browser and regular backups. Clearing all browser
cookies loses anonymous identities; a ZIP restores data, not device access.
The device that originally owned the workspace can be disconnected too. Do not
delete its Supabase anonymous Auth user: the durable workspace is still keyed
to that original identity. Pairing does not merge two existing libraries.

## Sync and security behavior

- A visible online tab uses one same-origin SSE connection. Render bridges it
  to Supabase Realtime with that browser's own JWT. The public browser never
  receives the JWT, refresh token, or Supabase credentials. Revision-only hints
  trigger incremental pulls; keepalives do not query application data.
- Hidden tabs close their connection and do not catch up. Pending local uploads
  remain queued and use the existing upload flow. Opening, becoming visible,
  reconnecting, and **Sync now** catch up even with an empty outbox. Subscription
  acknowledgement precedes catch-up; a cursor high-water mark covers changes
  that arrive while a response is in flight. There is no periodic data polling.
- Meal edits still send only the slot operation; photo bytes transfer only when
  new/replaced or missing on the receiving device. Operation receipts remain
  durable and unchanged. Conflicts retain local edits. Deleted recipes are not
  resurrected by stale updates. Open recipe editor drafts are not overwritten by
  incoming changes. The active app tab remains a browser-local preference.
- Every protected request checks membership, including cached photo reads.
  Database and Storage RLS also enforce membership. Workspace/epoch checks stop
  old queued requests from crossing a pairing switch. Disconnect serializes with
  writes and immediately revokes server access. A device signal closes an open
  workspace stream. A disconnected browser retains downloaded local data and
  edits, shows **This device has been disconnected**, and needs a fresh approved
  QR to rejoin. During fresh pairing it can receive only its own approval signal.
- Backups keep the existing versioned JSON/images format and latest local edits.
  Device membership, pairing tokens, session cookies and credentials are never
  exported or imported. Restoring data does not grant device access.

Render restarts/sleep, network loss, and provider outages can interrupt pairing
or live connections. The local editor stays usable; reopening/reconnecting
resumes queued writes and visible catch-up. Pairing requires the server to be
reachable, including any initial Render wake-up delay.

## Verification

Developer prerequisites: Python environment from `requirements.txt`, Node,
Playwright with Microsoft Edge, `ws`, and `@electric-sql/pglite` installed in
`.tools/sql-check/node_modules` (the existing SQL test setup). The HTTPS fixture
uses Git for Windows' OpenSSL for a temporary self-signed test certificate.

```powershell
node tests/devices_sql.cjs
node tests/devices_browser.cjs
python -m unittest discover -s tests
```

The browser suite uses independent IndexedDB stores and cookies on HTTPS, real
PostgreSQL functions/RLS, and explicit Auth/Storage/Realtime transport fixtures.
It verifies approval/code matching, cancellation with pending edits, images,
session-free ZIP export, pairing persistence, isolated workspaces, live meal
deltas without overwriting a draft, 35 seconds idle without data polling, hidden
connection closure and one catch-up, a write during a held catch-up response,
offline reload, identical lost-ACK retries, retained conflicts, active/offline
revocation and fresh rejoining. SQL tests also cover expired/reused tickets,
unpaired direct access, read-only photo staging, and revoked direct Storage RLS.

Live Supabase replication/Render and a physical iPhone must be checked after the
migration and environment flag are enabled. No provider fixture proves actual
Supabase publication configuration or an iPhone camera scan. On the live app use
two independent browsers; DevTools Network should show one `/api/sync/events`
stream per visible tab, small `/pull?since=…` responses on changes/visibility,
and no timed pulls while idle. Hide the first browser, save three changes on
the second and close it, then return to the first: expect one catch-up. Turn
network offline, save/reload, reconnect and inspect a single durable operation ID.
Repeat Disconnect both online and with an offline queued edit.
