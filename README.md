# Automated Signal Refresh — Setup

## What this adds
A GitHub Actions workflow that runs `fetch_data.py` on a schedule (every 12
hours by default), pulling live GDELT news-volume data for the six tracked
signal keywords, computing a real z-score against each one's own 60-day
baseline, and committing the result into `data.json`. Your dashboard already
reads `data.json` same-origin, so nothing on the frontend needs to change.

## Why this should work where the in-browser version didn't
Two separate reasons, not one:
1. **CORS**: browsers block cross-origin JS fetches. A GitHub Actions job isn't
   a browser, so this restriction doesn't apply at all.
2. **IP reputation**: when GDELT rate-limited your browser, it did so even on
   *direct URL navigation* — which bypasses CORS entirely. That proved the
   block was tied to your network's IP, not a CORS issue. GitHub Actions
   runners use Microsoft/GitHub's own IP ranges, essentially certain not to
   already be caught up in whatever flagged your ISP/mobile carrier.

## Setup
1. Copy `fetch_data.py` and `.github/workflows/refresh-signals.yml` into your
   repo, preserving the folder structure.
2. Repo Settings → Actions → General → set Workflow permissions to
   "Read and write permissions" (needed so it can commit the updated
   `data.json`).
3. Trigger it once manually: Actions tab → "Refresh live signal data" →
   "Run workflow". Watch the run log — you should see six `OK  <keyword>: ...`
   lines if GDELT responds, or `FAIL <keyword>: ...` if something's still
   wrong (paste that log if so, and I can debug the actual error rather than
   guessing again).
4. If it works, leave the schedule as-is or edit the cron line in the
   workflow file to change frequency.

## What's still manual, and why
- **WHO/CDC outbreak data, governance, AI-Bio, synthesis screening**: WHO's
  Disease Outbreak News page is JavaScript-rendered from an internal API with
  no public documentation — a script can't reliably scrape it without risking
  stale or wrong data. This content stays on the "ask Claude to check"
  workflow, same as it's been working.
- **ReliefWeb supplement (optional)**: the code is included and will pull
  additional outbreak-relevant reports as "needs review" entries, but
  ReliefWeb now requires a *pre-approved* appname (their Nov 2025 policy
  change) rather than any arbitrary string. Request one at
  https://apidoc.reliefweb.int/parameters, then add it as a repo secret named
  `RELIEFWEB_APPNAME` (Settings → Secrets and variables → Actions). Until you
  do, this step is automatically skipped — it will not block the GDELT update.

## Honesty note on testing
I could not verify this script's live GDELT call from my own environment —
my sandbox blocks unlisted domains for unrelated reasons (confirmed via the
`x-deny-reason: host_not_allowed` response header, same restriction that's
affected earlier scripts in this project). The script's logic, error
handling, JSON output, and request pacing are all verified correct by
running it against that same blocked response. What's genuinely unverified
is whether GDELT itself responds successfully from a real GitHub Actions
runner — please check the first live run's log and share it if anything
looks wrong.
