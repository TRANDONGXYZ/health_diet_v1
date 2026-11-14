# Meal Notebook — local Windows app

A small recipe library and weekly planner. The browser UI uses plain JavaScript and IndexedDB for offline-first saves; the Python server syncs data to SQLite locally or Supabase when hosted. No Node.js, frontend build, subscription, or cloud AI key is needed.

For the **Render Free public beta**, see [DEPLOYMENT.md](DEPLOYMENT.md): GitHub `main` deployment, server-side Groq, Supabase anonymous tester storage, backup migration, environment variables and verification. Running `start.cmd` still uses the local app and Ollama.

Manual changes now save in the browser first and sync in the background. Once loaded online, the app reopens offline on the same device. See [OFFLINE.md](OFFLINE.md) for the migration, backup behavior and verified timings. **Existing hosted deployments must run `supabase/offline.sql` before deploying this update.**

## Start on Windows 11

1. Install **Python 3.11 or newer** from [python.org](https://www.python.org/downloads/windows/). Select **Add Python to PATH**. Python 3.15 is also supported by the core app and the installed caption dependencies.
2. Install or update [Ollama for Windows](https://ollama.com/download/windows), then open Ollama. It runs in the system tray. No NVIDIA GPU is required; CPU inference is supported.
3. In this folder, double-click **`setup.cmd`**. This creates an isolated `.venv`, installs video readers and bundled FFmpeg, and downloads the local Whisper CPU speech tool and multilingual base model (about 150 MB for Whisper, plus the FFmpeg package). Internet access is needed for this step. If upgrading an existing installation, rerun this script with the app stopped.
4. Open PowerShell and download the models once:

   ```powershell
   ollama pull qwen2.5:1.5b
   ollama pull llama3.2:1b
   ```

5. Double-click **`start.cmd`**. It opens **http://127.0.0.1:8765** in your browser. Keep the server window open for AI and background sync; previously loaded manual tools work even when the server is stopped. Press **Ctrl+C** in that window to stop it.

You can also run these steps manually from PowerShell in this project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m mealapp.speech --setup
.\.venv\Scripts\python.exe app.py --open
```

Activation of the virtual environment is unnecessary, so PowerShell execution-policy changes are unnecessary. Without the caption dependencies, `python app.py --open` still supports manual recipes, article import, pasted transcripts, nutrition, and planning.

## Model choice and performance

Recipe extraction automatically detects Vietnamese or English from the **Article text or video transcript** field, without an AI detection request. It processes that original text directly:

- **Vietnamese:** `qwen2.5:1.5b` (about 1 GB). [Qwen documents Vietnamese support](https://qwenlm.github.io/blog/qwen2.5/).
- **English:** `llama3.2:1b` (about 1.3 GB). [Llama 3.2 documents English support](https://ollama.com/library/llama3.2).
- Weekly planning and nutrition helpers: `qwen2.5:1.5b` by default.

**The interface stays English. Extracted recipe content stays in the source language by default.** Vietnamese sources keep Vietnamese names and quantities; English sources keep English names and quantities. There is no automatic translation pass. Existing saved recipes are not translated. Mixed text uses the dominant detected language; the detected language and selected model appear below the source field. Very short, ambiguous text can be misclassified; include the recipe's ingredient list and relevant source text. Settings has separate fields for Vietnamese extraction, English extraction, planning and ingredient lookup. Install any replacement model before selecting it.

Extraction makes **one short recipe request** for facts and a flat ingredient list. Cooking-step generation and ingredient grouping have been removed. Calorie arithmetic, filtering, labels, and editing do not call AI. Unknown foods still need online lookup. This reduces inference work, but model loading, longer transcripts, and speech transcription can still take minutes on an older CPU. The app keeps inference serial, limits text context to 8,192 tokens. Close other memory-heavy applications if necessary.

Small models can omit or misread facts. Review ingredient names, amounts, vegan status and servings before saving. Each extracted ingredient must include a matching supporting source excerpt. Ingredients without matching names/evidence are rejected with a review warning. This reduces unsupported additions; it does not guarantee perfect understanding of a source. Structured article ingredient rows are retained even when omitted by the model. A local check also recovers clearly stated g/kg amounts next to common ingredient names, without another model request.

For advanced settings, copy `config.example.json` to **`config.local.json`**. You can adjust the context size, AI timeout, or Ollama address. The default `ai_timeout_seconds` is **900 seconds (15 minutes)** for the whole model request; `ai_read_timeout_seconds` is **180 seconds without incoming data**, including initial model loading/prompt reading. Ollama responses are streamed internally so a model that is still generating is not mistaken for an unavailable service at four minutes. The busy message shows loading/generation status and elapsed time. If an older `config.local.json` sets `ai_timeout_seconds` to 240, update that override to 900. Settings saved in the UI take precedence for model names and the USDA key. Recipe language is detected automatically; older fixed-language settings are ignored. Credentials stay on this laptop; `config.local.json` and `data/` are ignored by Git.

## Try the complete workflow

### Create a dish

Open **Dish List → Create recipe**. **Retrieve source** is temporarily disabled for all source types. Paste a transcript or recipe text, or upload a transcript/audio/video file, then click **Extract recipe with AI**. Manual entry remains available. Cancel and Save Recipe stay in the fixed header while the editor body scrolls; Recipe Details keeps Edit in its header too. Closing a dialog clears its errors and discards feedback from requests belonging to that opening.

- Articles with Schema.org Recipe data use their ingredient and instruction fields, avoiding surrounding blog text. Other articles use readable HTML text.
- YouTube retrieval tries Vietnamese then English captions, including automatic captions. Other captioned video sources can be tried with **Source type → Other video**, using yt-dlp.
- **Facebook videos use the same Source link field and automatic source mode.** Supported video URL shapes include `facebook.com/watch/?v=...`, `facebook.com/.../videos/...`, Facebook Reels, `facebook.com/share/v/...`, `facebook.com/share/r/...`, and `fb.watch/...`. Captions are tried first; otherwise the app downloads audio and transcribes locally, then offers the same **Extract recipe with AI → review → Save recipe** workflow. Public videos must be accessible without signing in. Private/restricted/deleted videos or Facebook download blocks produce an actionable error; the app never reads browser cookies or bypasses access restrictions.
- If captions are missing, empty, blocked, or contain a placeholder such as **“Caption is updating ...”**, the app automatically downloads the video's audio and transcribes it locally. It uses [whisper.cpp's Windows CPU build](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.8.3), the [multilingual Whisper base model](https://github.com/ggml-org/whisper.cpp/blob/v1.8.3/models/README.md), and FFmpeg to convert the audio to mono 16 kHz PCM. This works with the existing Python 3.15 installation without requiring PyTorch, CUDA, or a cloud speech service.
- The generated transcript appears in the same source-text field as article text. Review it, then click **Extract recipe with AI**; it goes through the same recipe extraction and review workflow. Speech recognition can mishear Vietnamese ingredient names, weights, and serving counts, so check them against the video before saving.
- Audio processing can take several minutes on an older CPU. Whisper uses up to four CPU threads by default, and its model is downloaded only once. Successful audio transcripts are cached in the local database. Downloaded audio, converted WAV files, and temporary transcript files are deleted after both successful and failed jobs.
- Audio downloads/transcription cannot work for every link: a video may be private, removed, live, require sign-in, or block automated downloads. The app reports an error in these cases and still accepts pasted text. Paywalled and JavaScript-only articles may also require pasted text.
- Long sources should be trimmed to ingredients and instructions, under 16,000 characters, for the small model.
- Review the single **Ingredients** list: name, original quantity, and editable weight in grams. Hover over a name to inspect its source excerpt; saved recipe details also expose **Source evidence**. Quantities refer to the entire recipe. Explicit g/kg weights are checked locally; count/volume conversions are marked estimated. Unknown amounts remain blank.
- No cooking instructions or step assignments are generated. Use **View original source** for the cooking method; original article text/transcript remains in the editor.
- The app checks known ingredients' vegan classifications; you retain control of the saved status. Unknown ingredients require review. Servings missing from the source stay blank, so per-serving calories are unavailable until you enter them.
- Enter measured cooking time if known. Source-stated time can be retained, but no separate cooking-time inference is requested during import or save.
- Mark **Good for Running** if you want the dish prioritized for running days. This is your own label; the app does not infer exercise suitability. Edit this saved label in the recipe editor; its existing filter can still be combined with vegan filtering and search.
- Click **Save recipe**. Draft extraction alone does not add a dish to the library.

Existing recipes open as a flat list without changing their ingredients, weights or calorie references. Old cooking groups/instructions remain archived in the local database and JSON export (`legacy_groups`) so they are not lost; the editor and details no longer generate or show cooking steps.

### Calories and your own measurements

The app starts with **86 common ingredients**, including English and Vietnamese aliases, with explicit raw/cooked entries where relevant. Starter values are approximate reference values, not newly verified laboratory measurements. Use product labels or your own values for the foods you actually buy.

Calories are calculated deterministically:

```text
recipe kcal = sum(ingredient grams × ingredient kcal per 100 g / 100)
kcal per serving = recipe kcal / servings
```

AI extracts and, when necessary, estimates ingredient weights; it does not perform the arithmetic. Estimated weights are marked. An unknown weight or unknown calorie value makes the total **partial**, rather than silently pretending the ingredient contains zero calories. A known zero-calorie ingredient, such as water, counts normally. Cooked dish weight and kcal per 100 g of the finished recipe cannot be inferred reliably from raw weights, so the app reports whole-recipe and per-serving calories.

Editing a new ingredient name triggers a lookup if it is not in the library. On import/save, unknown names are also resolved automatically. Only an unknown food lookup may translate its search term locally into English for USDA; this does not translate or rename the recipe or its source. The first ranked result, preferring a matching raw/cooked state, is saved with its exact USDA description and source link. **Review automatic matches** in **Food calorie values**: ingredients with similar names, brands, and preparation states can have different energy values. No value is invented when lookup fails.

Use **Food calorie values**, also available inside the recipe editor, to add or edit kcal per 100 g and aliases. Your value overrides the reference for all recipes using that food. Calorie totals and saved weekly cards update automatically. Your cooking weights should refer to edible portions; separately account for oil left in the pan or discarded ingredients if those materially affect your measurement.

### Plan your week

**Weekly Meals** shows the current Monday-Sunday date range and its saved plan, followed by created future weeks in date order and past weeks in reverse date order. Dates follow the laptop browser's local timezone. For the current week, generation covers **today through Sunday inclusive**: Monday generates seven dishes, Thursday four, and Sunday one. Each created future week has its own generation controls, which fill all seven days and update only that week. Returning to the tab loads its saved plan without making an AI request. Generate with AI remains visible but disabled.

Choose **Vegan dishes only** and/or **Prioritize Good for Running**, then choose:

- **Generate with AI:** temporarily disabled for every week. It stays visible and sends no generation request.
- **Simple Rotation:** immediately save a rotation of matching saved dishes, without AI.
- **Randomize:** immediately save a shuffled selection of matching saved dishes, without AI. Dishes repeat when needed to fill the remaining days.

There is no preview or confirmation popup. The available generation options update only their own week and leave other weeks intact. For the current week, they replace only today through Sunday; earlier dates keep their exact saved meals, snapshots and empty slots. Earlier meals can still be changed manually. The running preference includes available marked dishes before unmarked ones (up to the number of days). Vegan filtering still applies; a notice explains when no matching dishes have the running label.

For an AI job already running before the button was disabled, **Cancel** stops the streamed Ollama request and its worker, freeing the inference CPU. The existing saved plan stays visible and remains saved if generation fails or is cancelled. The model may remain loaded in RAM for its normal idle period; it no longer generates tokens. Reloading the page reconnects to an active job so you can still cancel it. Restarting the app retains completed plans and does not resume unfinished jobs.

Each meal card shows its date, dish image, cooking time, name and calories per serving. Today's meal has a thick green outline. Hovering other cards uses a thin border and keeps photos at full brightness. Click the main card area, or focus it and press Enter, to open dish details and preparation advice. Linked cards and details in every week use the latest saved recipe by ID, including offline edits. Dates and each meal's saved serving count stay unchanged; original snapshots remain a fallback for unavailable recipes. An old saved singleton plan is migrated into weekly history automatically on first access. History is retained in the browser and synced to the local/hosted database, with one record per week.

**Delete a dish:** in Dish List, click the trash icon (**Delete recipe**) and confirm the warning. Cancelling or dismissing the confirmation changes nothing. Confirming removes the dish from Dish List and marks every occurrence across past, current and upcoming weeks as **Dish deleted**. Each scheduled date and slot stays in place, without the old photo, recipe details or preparation advice. This state survives reloads and backups. Other dishes and empty slots stay unchanged. Use the slot's swap icon (**Replace meal**) to replace a deleted dish with a saved dish.

**Replace a meal:** click the swap icon (**Replace meal**) on any meal card, including in past weeks. The popup lists all saved dishes, regardless of the plan's generation filters. The first displayed dish is selected and previewed automatically. Opening the picker saves nothing; select a different dish if needed. Click **Replace** to update only that date, or Cancel to leave it unchanged. The replacement gets a fresh recipe snapshot and generic preparation guidance; other dates keep their existing snapshots. A non-vegan replacement clears the week's vegan-only label.

**Delete a week:** click the trash icon (**Delete week**) beside any week's heading to remove that entire plan. Saved recipes and photo files remain available. While AI is generating for a week, cancel it before editing or deleting that week; other weeks can still be changed. Past weeks support manual replacement and deletion, with no generation controls.

**Collapse weeks:** use the small arrow in any past or upcoming week's header. Each week's expanded or collapsed choice is saved separately and restored after reload. Weeks without a saved choice start expanded. The current week always stays expanded and has no collapse arrow, including when a previously collapsed upcoming week becomes current.

**Plan ahead:** click **Add Week**, select a Monday-Sunday range, then click **Add**. Add receives keyboard focus when the dialog opens, so pressing Enter immediately adds the selected week using the same validation. You can create any of the next **eight** weeks; the current week is additional to this limit. For example, on Wednesday November 11, 2026, the next week is November 16-22. Each new week has seven empty slots. Click a **+** slot to browse all saved dishes, expand a dish's details, and click **Add**. You can also use that week's Simple Rotation or Randomize controls to fill all seven days. Future weeks appear in date order above past weeks. When a future week becomes current, new generation follows the today-through-Sunday rule.

**Recipe editing from meals:** Recipe Details offers Edit for any date when the linked recipe still exists. It opens the same Dish List recipe, without replacing the meal. Saving returns to the originating tab, week and scroll position. Deleted dishes keep their deleted marker. The active tab is remembered on this device and restored before either panel is shown, including offline reloads.

**AI meal generation:** Generate with AI is temporarily disabled for the current and upcoming weeks. Simple Rotation and Randomize remain available. Recipe extraction with AI is unchanged.

**Household servings:** Settings lets you set a positive whole number or leave the default blank. Recipe Servings always describes the original yield. Every newly assigned meal (manual Add/Replace, Simple Rotation or Randomize) saves its own quantities using `household / original yield`; units are kept and unmeasured text such as "to taste" stays as written. Missing serving counts leave quantities unchanged. Changing the default affects only later assignments. Editing the original recipe recalculates every linked meal using that meal's saved serving count. Planned measurable quantities display with exactly one decimal place; original recipe quantities and full calculation precision stay unchanged. ZIP backups include the default and the saved planned quantities, including pending local changes. This update requires no Supabase SQL changes.

**Meal picker:** Cancel, Add/Replace and Close stay in the fixed header. The saved-dish list and recipe preview scroll independently; the dialog frame and background page stay in place. Both Add and Replace automatically preview the first displayed dish, but save nothing until their primary action is confirmed. Add stays disabled when there are no saved dishes. Press `/` to focus Search saved dishes; while typing in a field, `/` remains a normal character.

All dialogs keep a fixed height within the viewport and scroll longer content internally. The page behind a dialog stays at its original scroll position; closing the dialog after Add or Replace returns you to the slot you updated.

**Dish photos:** in Create recipe or Edit recipe, select a `.jpg`, `.jpeg`, or `.png` file (up to 20 MB). The browser applies its image orientation, center-crops a square and resizes it to exactly **500 by 500 pixels**, storing a PNG on this laptop. A preview appears before saving. Photos can be replaced or removed from a recipe; all linked meals immediately show the updated photo, including past weeks. Recipes without photos use a neutral square placeholder. Photo processing uses the browser's canvas and standard-library server validation, with no extra image package to install.

The planner uses a varying pool of up to 12 eligible saved dishes and short ingredient summaries to keep model requests manageable. This plans one dish per day, not a complete daily diet or calorie target. See the linked [FoodSafety.gov storage guidance](https://www.foodsafety.gov/food-safety-charts/cold-food-storage-charts) when preparing food ahead.

## Online nutrition access

The app uses the [USDA FoodData Central API](https://fdc.nal.usda.gov/api-guide/) with `DEMO_KEY` by default. Its documented limits are **30 requests/hour and 50/day per IP address**. Searches are cached on disk, and successful ingredient matches are stored permanently. For regular use, get a [free USDA API key](https://fdc.nal.usda.gov/api-key-signup) and enter it in Settings.

The API returns reference nutrition per 100 g. Nutrient ID 1008 is preferred for energy, with Foundation Foods' IDs 2048/2047 supported where needed. Only generic SR Legacy/Foundation records are used, avoiding branded serving-size confusion. Values vary across records and food states; the app shows the actual selected record.

The website runs locally, but source retrieval, missing-food lookups, and ingredient nutrition lookups need the internet. Saved recipes, weekly history, dish photos, manual editing, Simple Rotation and Randomize work offline. Local AI works offline after models have downloaded. Search queries and food names are sent to online data providers; images are not.

## Storage, backup, and troubleshooting

- Recipes, food values, settings, weekly plans, photos and pending operations save first in **IndexedDB**. Closing/reopening the browser preserves them. Background sync updates local SQLite/photos or hosted Supabase; keep cookies to retain the hosted identity. Export a backup before clearing browser site data, especially while changes are pending.
- **Settings ? Export Backup** downloads a single ZIP containing recipes, food values, all weekly plans (including empty future slots), each week's saved expanded/collapsed choice, uploaded dish images and all saved/effective settings, including pending local edits, including advanced configuration and API keys. The footer's Export backup action downloads the same full ZIP.
- **Settings ? Import Backup** asks for confirmation after you select a file: "Importing this backup will replace all current data with the contents of the backup file. Do you want to continue?" Cancelling or closing the confirmation leaves all current records, images, settings and collapse choices intact. After confirmation, the app validates the versioned JSON, required tables, record counts, dates, collapse choices, image references, image hashes and PNG data before the browser changes local data. Invalid, corrupt or incomplete archives leave existing data intact. Imports wait for you to finish/cancel active AI tasks; one IndexedDB transaction applies the local replacement and outbox entries; a failed local transaction keeps the old data. Background sync stages images and restores the server records atomically. Restored collapse choices still leave the current week expanded; older backups without these choices default to expanded weeks.
- Older meal snapshots may retain grouped ingredients without a flat list, including snapshots copied from legacy recipes after the offline-first upgrade. ZIP export/import adds the missing list from the snapshot's own groups first, then the linked saved recipe if needed. If no ingredients were recorded anywhere, it keeps an empty list rather than inventing amounts. Orphaned snapshots retain their names, notes, group instructions, calories, dates and photos. Linked meals use the current saved recipe and their saved serving count. Export does not modify stored records or pending sync entries. Export feedback identifies recovered meal names/dates, and `HealthBackup.exportRepairs` exposes the full diagnostic list in the browser console.
- Backup version 1 contains `backup-v1.json` and an `images/` folder. Photo URLs use the same content-derived filenames after restore, so recipe and historical plan images reconnect automatically. Limits are 256 MB per ZIP, 512 MB expanded, and 64 MB for the structured JSON. Backups are unencrypted and contain API keys; keep the ZIP private. Installed Ollama/Whisper models, binaries and the application code are separate from user data and are not bundled; use the setup instructions on another laptop before restoring.
- Use the UI ZIP export for a complete current backup, including pending edits. A manual copy of `data/` plus `config.local.json` contains only changes that have reached the local server.
- The server binds only to `127.0.0.1`. Keep it local; it is not designed for public hosting or multiple users.
- **Port busy:** run `.\.venv\Scripts\python.exe app.py --port 8766 --open`.
- **Ollama offline:** open Ollama. If its app is not running, `ollama serve` starts its server. Do not start a second copy if the tray app already serves it.
- **Model missing:** run `ollama list`, then `ollama pull` with the exact model name shown in Settings.
- **Slow AI/timeout:** the first request includes model loading; Vietnamese transcripts can take several minutes on CPU. The app allows 15 minutes by default and shows progress while receiving the response. A timeout identifies either the total processing limit or a period without data, separately from a failed connection, missing model, or incomplete model output. Your source text stays in the editor so you can retry. Increase `ai_timeout_seconds` or `ai_read_timeout_seconds` in `config.local.json` if needed, then restart. Only one inference job is accepted at a time.
- **Incomplete AI output:** retry with shorter text or a more capable model. Failed extraction/planning is not silently replaced with invented recipes.
- **Captions fail:** rerun `.\.venv\Scripts\python.exe -m pip install --upgrade -r requirements.txt`, or paste the transcript. No sign-in credentials or browser cookies are required or automatically read.
- **Audio fallback setup:** rerun `setup.cmd` with the app stopped, then restart using `start.cmd`. The first audio fallback also downloads missing Whisper assets automatically when internet access is available. If Windows reports missing runtime DLLs, install the [Microsoft Visual C++ x64 redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist).
- **Speech recognition language/quality:** `config.local.json` supports `speech_language: "auto"` (default), `"vi"`, or `"en"`. Set `"vi"` for consistently Vietnamese videos if language detection struggles. `speech_model` can be `"tiny"` (75 MiB, faster/less accurate), `"base"` (142 MiB, default), or `"small"` (466 MiB, slower/more accurate in general). Models must be multilingual, without `.en`; the selected model is downloaded on setup/first use. These are separate from the Ollama recipe models.
- **Long audio:** the default limit is 60 minutes and 200 MB of compressed audio per video; live streams are rejected. `speech_max_video_minutes`, `speech_timeout_seconds` (default 1,800), `video_download_timeout_seconds` (default 300), and `speech_threads` (default 4) can be changed in `config.local.json`. Over-limit audio is rejected, rather than silently returning a truncated transcript. The existing 16,000-character limit for AI recipe extraction still applies; trim long transcripts to the relevant recipe before extraction.
- **USDA rate limit:** add a personal key, wait for the limit to reset, or use a manual value. Offline errors leave calories visibly incomplete.
- **“python not found”:** reopen the terminal after installation and check `python --version`. The scripts use `python`, not the Microsoft Store launcher. If needed, use a Python installation's full executable path.

## Verification

Run the fast standard-library tests:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

They cover calorie math, serving changes, raw/cooked matching, Vietnamese aliases, zero/missing values, manual overrides, persistence, source-evidence validation and mass conversion, weekly repetition and ID validation, source extraction, caching, local HTTP behavior, and request protection. AI transport tests exercise a local fake Ollama server to check streamed Unicode, active responses longer than the idle limit, total/idle/connect timeouts, model failures, partial responses, progress, and lock cleanup. Speech tests cover missing/placeholder captions, caption-first behavior, local audio conversion/transcription, shared recipe import, caching, download integrity, timeouts, and cleanup. Facebook tests cover link detection and routing through the shared caption/audio workflow. Language tests cover native Vietnamese/English model routing, automatic detection, preserved source names, rejection of unsupported ingredients, no cooking/translation calls, and legacy saved groups. Running tests cover label persistence, combined vegan preferences, bounded candidate pools, and priority enforcement when a model ignores the flag. Tests use temporary databases and do not change your dishes.

Optional live checks (use real local models/internet; allow time and USDA quota):

```powershell
.\.venv\Scripts\python.exe tests/live_web.py
.\.venv\Scripts\python.exe tests/live_ai.py
.\.venv\Scripts\python.exe tests/live_video.py
.\.venv\Scripts\python.exe tests/live_speech.py
.\.venv\Scripts\python.exe tests/live_facebook.py "https://www.facebook.com/share/v/VIDEO_TOKEN/"
.\.venv\Scripts\python.exe tests/live_recipe_language.py
```

After retrieving a Facebook video, you can run `tests/live_recipe_language.py --facebook-url "THE_SAME_FACEBOOK_URL"` to check native-language extraction of its cached transcript. This also checks an English article and Vietnamese text against their respective local models; it reports counts/timings without saving recipes.

`live_speech.py` deliberately bypasses captions on a public 19-second video to verify audio download and real local speech recognition. Pass `--url "https://www.youtube.com/watch?v=..."` to test a specific video; a long video may take several minutes. `--sample` instead downloads whisper.cpp's short reference WAV and tests audio conversion/recognition without YouTube access. These checks do not save a recipe.

To retry the longest locally cached audio transcript against Ollama, run `.\.venv\Scripts\python.exe tests/live_ai.py --cached-source --recipe-only`. It reports timings/counts without printing the source/recipe contents or saving a dish.

`tests/browser.cjs` is an optional Playwright check using installed Microsoft Edge. It exercises the two tabs, recipe CRUD, immediate recalculation, food overrides, saved running labels and filters, priority persistence, language routing, remaining-week scheduling, clickable meal cards, persistence, and narrow-screen layout. Node.js and `playwright` are needed **only** for that developer check, not to run the app. Browser test data is isolated in a temporary directory; screenshots go to ignored `test-results/`.

The weekly regression checks cover Monday/Thursday/Sunday boundaries, client timezones, fresh inference on every click, history snapshots, date-specific replacements and week deletion, cancellation before persistence, week rollover, and photo validation. The browser fixture uses an explicit AI stub and a fixed Thursday; it exercises real HTTP/storage, image cropping, cancellation/reconnection, history ordering, editable meals, week deletion, bright hover and today highlighting:

```powershell
node tests/weekly_browser.cjs
.\.venv\Scripts\python.exe tests/live_cancel.py
.\.venv\Scripts\python.exe tests/live_weekly.py
```

`live_cancel.py` starts a synthetic long response on your real local Ollama server, cancels it, and checks that a fresh short request can run promptly. It does not save a recipe or plan. Browser checks need Playwright and Microsoft Edge; these are developer-only dependencies.

`live_weekly.py` generates a real AI plan for the remaining days, verifies immediate saving and fresh inference on every click, and discards its temporary database.

Advance-planning and full-backup checks:

```powershell
node tests/future_backup_browser.cjs
node tests/behavior_browser.cjs
node tests/deletion_browser.cjs
```

This uses a fixed Wednesday and an explicit AI fixture to check empty slots, independent per-week controls, the eight-week limit, past-week collapse, and a real ZIP round trip that restores images, settings, recipes and every week. Unit tests also simulate missing/corrupt archive entries and a database failure to verify rollback.

The behavior check additionally verifies initial Add focus and Enter validation, persisted past/upcoming collapse choices, always-expanded current weeks, stable dialog heights and background scroll locking, exact scroll restoration after Add/Replace, preservation of earlier filled and empty slots for all generation methods, and backup confirmation/cancellation with collapse-state restoration. Tests isolate user data and local configuration in temporary directories.

The deletion check verifies confirmation before any deletion request, unchanged saved data after cancellation/dismissal, repeated deleted occurrences across past/current/upcoming weeks, hidden old photos and details, persistence after reload, and replacement of deleted slots. Unit tests also verify atomic rollback on failure, legacy-plan migration, and backup restoration of deleted slots.

Meal-picker and legacy-backup regression check: `node tests/picker_backup_browser.cjs`. It covers fixed columns/header, first-dish preview without a save, keyboard search, visible errors, existing and newly saved legacy snapshots, and a ZIP round trip with photos/settings/ingredient values and pending edits.

Linked-recipe regression check: `node tests/linked_recipes_browser.cjs`. It verifies offline name/ingredient/yield updates across all dates, saved serving counts, recipe-only sync, live open dialogs, reload, original recipe quantities, exact one-decimal display without calculation rounding, deletion markers and disabled AI controls with no generation requests.

**Card actions and search:** Dish List uses a pencil icon to edit and a trash icon to delete; saved running labels remain editable in the recipe editor. The existing running filter is unchanged. In Dish List with no dialog open, press `/` to focus Search dishes without inserting a slash. While typing in an editable field, `/` remains a normal character. The meal picker keeps its own search shortcut.

Targeted regression: `node tests/actions_search_browser.cjs` covers keyboard icon actions, preview-only first selection, cancellation and date-specific replacement, both search shortcuts, running-label persistence, ingredient lookup/calories, and a recipe-photo backup/restore while edits are pending.
