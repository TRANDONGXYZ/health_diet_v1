# Idle flicker investigation and verification

The confirmed trigger was `app.js`'s unconditional 60-second `refresh()` timer.
It read IndexedDB and called `renderState()`, which assigned `innerHTML` to both
card lists and refreshed any open recipe/picker details. Window focus called
the same refresh. Reassigning cached blob image URLs to newly created image
elements produced new `load` events without HTTP image downloads or navigation.
Realtime keepalive comments did not trigger these updates.

Before editing runtime code, a 185-second desktop trace reproduced three full
card-tree replacements, at approximately 60, 120 and 180 seconds. The same
baseline was reproduced on the deployed Render site with an iPhone 13 viewport.
Both Weekly Meals and Dish List were monitored in separate idle pages.

| Per page, over 185 seconds | Before | After |
| --- | ---: | ---: |
| Full card-tree rebuilds | 3 | 0 |
| Existing card nodes removed (2 meal + 2 dish cards) | 12 | 0 |
| Existing image `load` events | 6 | 0 |
| Page navigations | 0 | 0 |
| Sync requests during idle | 0 | 0 |

## Focused fix

- Remove the one-minute UI refresh. Focus checks only for a real date change.
  A single next-midnight callback retains date/week rollover, including offline
  visibility return, without polling or rebuilding unchanged content.
- Keep state and record versions current on every acknowledgement, while
  comparing saved view values independently of metadata and JSON key order.
  Empty deltas and version/cursor-only acknowledgements do no card rendering.
- Reconcile existing templates by recipe ID and scheduled week/date. Unchanged
  cards, images, week containers and dialog content retain their DOM nodes.
  Changed names, quantities, attributes and image sources update in place.
- Keep realtime notifications, incremental pulls, reconnect/visibility catch-up,
  the offline outbox, database schema, styles and deployment settings intact.

## Regression checks

With the existing developer Playwright/Edge and PostgreSQL fixture setup:

```powershell
node tests/flicker_browser.cjs --after --engine edge,edge-mobile --idle-ms 185000
node tests/flicker_updates_browser.cjs
node tests/flicker_updates_browser.cjs --mobile
```

The first test checks both views in visible desktop and mobile-sized pages for
three minutes and five seconds. It records render stacks, removed nodes, image
load events, navigation and sync traffic in ignored `test-results/flicker-*.json`.
`--before` is for reproducing the old deployed version before upgrading.

The paired-session test checks a Friday replacement, retention of every other
card/image and an unrelated open details image, one rendering pass for a local
save with no second pass on acknowledgement, a cursor-only empty delta,
one empty catch-up on visibility return and on reconnect, and offline date/week
rollover. It uses two independent browser identities with actual QR approval.
Live checks can be explicitly selected with `--url https://health-diet.onrender.com`;
they create isolated anonymous workspaces and clean up their test recipes/plans.
No AI requests are used.

Microsoft Edge on Windows 11 was tested at desktop size and with the iPhone 13
device profile (390 × 844). This is Chromium emulation, **not physical Safari**.
Windows Application Control blocked the downloaded Playwright WebKit executable,
so no Safari/WebKit or physical iPhone pass is claimed.

## Verify on the iPhone

1. After deployment, reload Health Diet in Safari, keeping its cookies and saved
   data. If the previous cached version remains, close/reopen the tab and reload
   again. Let the photos finish appearing and wait for pending edits to sync.
2. Keep Weekly Meals visible for **five minutes**, with all paired devices idle.
   Repeat in Dish List; also leave a recipe's photo visible in Recipe Details.
   Recording the screen helps catch a brief flash. Photos and existing content
   should remain steady across the old one-minute boundaries.
3. On the paired computer, replace one Friday meal with a different saved dish.
   Wait for the computer's upload to finish. The iPhone should update that meal;
   unchanged cards/images and an unrelated open details photo should stay put.
4. Without editing anything, switch away from Safari for 30 seconds and return.
   Repeat after briefly disabling/re-enabling networking. Catch-up should complete
   without blanking/rebuilding the displayed cards or unchanged images.

For network inspection, desktop DevTools or Safari's remote Web Inspector should
show a single live events connection per visible tab. Keepalives may appear in
that stream; idle should produce no timed `/api/sync/pull` requests. Returning or
reconnecting performs one catch-up. An empty result keeps existing DOM nodes.
