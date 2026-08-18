# myNSE200 — Running on GitHub Instead of Your Laptop

This makes your nightly signals and Telegram replies run on GitHub's
servers, completely independent of your Mac — same as you already did
for your other project. Nothing about the STRATEGY changed; only where
it runs.

## Important — check this first, before uploading anything

Your `market_data.db` likely has ~20 years of history, which is almost
certainly too large to drag-and-drop through GitHub's website (that
upload method caps around 25MB per file). **Do NOT try to upload your
existing `market_data.db`.** Instead, this setup lets the very first
GitHub-hosted run create its own small, fresh database — it only needs
about 500 days of history to work correctly for live signals, not your
full backtest archive. Your full local database on your Mac is untouched
and still there for whenever you want to run more backtests.

## 1. Create a new private repository

Same as before: github.com → **+** → **New repository** → name it
something like `myNSE200-live` → **Private** → **Create repository**.

## 2. Upload your project files

**Do NOT upload these:**
- `market_data.db` (too large, and not needed — see above)
- `.env` (your Telegram token goes into GitHub Secrets instead, step 4)
- `venv/` folder (not needed — GitHub installs fresh each run)
- `output/` folder contents (will be created automatically)

**Do upload everything else**: all the `.py` files, `requirements.txt`,
the `data/` folder (just `nifty200.csv` and `sector_map.csv` — small,
fine to upload), and `.gitignore`.

Click **Add file** → **Upload files**, drag in those files, **Commit
changes**.

## 3. Create the two workflow files

Same two-step process you already know:
- **Add file** → **Create new file** → name it exactly
  `.github/workflows/nightly.yml` → paste the contents of the
  `nightly.yml` file from this package → **Commit changes**
- Go back to the repository root first (click the repo name in the
  breadcrumb), then repeat for `.github/workflows/listener.yml`

(Remember the lesson from before: always click back to the repository
root before creating each new file, so the folder path doesn't nest
inside itself.)

## 4. Add your Telegram credentials as Secrets (not as a file)

This is different from your other project's approach, and more secure:
- Go to **Settings** tab → **Secrets and variables** → **Actions**
- Click **New repository secret**
- Name: `MYNSE200_TG_TOKEN`, Value: your real bot token → **Add secret**
- Click **New repository secret** again
- Name: `MYNSE200_TG_CHAT_ID`, Value: your real chat ID → **Add secret**

Secrets are hidden even from you once saved — GitHub only lets the
workflow use them, never displays them again. This is the secure way to
handle this, better than putting a token in a plain `.env` file.

## 5. Grant write permission (same as before)

**Settings** → **Actions** → **General** → scroll to **Workflow
permissions** → select **Read and write permissions** → **Save**.

This is required because the nightly job needs to save its database and
reports back into the repository after each run.

## 6. Test it manually

**Actions** tab → click **myNSE200 Nightly Signal Run** in the sidebar →
**Run workflow** → **Run workflow** (confirm).

**The first run will take longer than usual** — it's doing the one-time
500-day seed backfill before running the actual strategy. Expect several
minutes, not the usual quick run. Watch for "Queued" → "In progress" →
green checkmark, exactly like before.

Once it succeeds, check your phone for the Telegram message, and check
the repository — you should now see a `market_data.db` file appear
automatically (committed back by the workflow itself).

## 7. Test the reply listener too

**Actions** tab → **myNSE200 Telegram Reply Listener** → **Run workflow**
→ confirm. This one should finish much faster (it's not fetching prices,
just checking Telegram).

## Verifying nothing about the strategy changed — same technique as before

```bash
diff ~/Documents/myNSE200/config.py ~/Downloads/config.py
diff ~/Documents/myNSE200/strategy.py ~/Downloads/strategy.py
diff ~/Documents/myNSE200/scoring.py ~/Downloads/scoring.py
diff ~/Documents/myNSE200/backtest.py ~/Downloads/backtest.py
```

(Download each file from GitHub's "Raw" view first, same as you did
before.) No output on any of these = confirmed identical, nothing about
what the strategy decides has changed — only `main.py` gained one new
optional flag (`--seed-days`, for that first-run backfill only) and two
new small files (`check_replies.py`, and the two `.yml` workflow files)
were added.

## What runs automatically from here

- **11:30 PM IST every night**: full signal run, same as your Mac did —
  fetches prices, refreshes fundamentals, scores everything, sends your
  Telegram report, saves the updated database back to the repository
- **Every ~10 minutes, all day**: checks for BUY/SKIP replies and
  processes them promptly

All of this now runs whether your laptop is on, asleep, or closed —
completely independent of your Mac, exactly like your other project.
