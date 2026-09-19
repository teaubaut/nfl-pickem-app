# NFL Pool Picks

A zero-cost, automated tool for a weekly NFL straight-up (win/loss) office pool.

It blends vig-free sportsbook moneylines with two prediction markets, compares the result against public betting splits, and serves everything as a mobile-first web app you can add to your iPhone Home Screen. Everything runs on GitHub Actions and GitHub Pages, with no server and no Python dependencies.

## What it does

**Blended win probabilities.** Vig is removed from each sportsbook's moneyline, then averaged with Kalshi and Polymarket midpoints. Weights are adjustable, and missing sources drop out of the average instead of breaking it.

**Contrarian and upset flags.** Public moneyline ticket percentages from Action Network are compared against the blended probability. A close underdog that almost nobody is backing gets flagged, which is where a swap can win you the week.

**Monday night tiebreaker.** Projects a final score from the spread and total, then snaps both numbers to common NFL scores (17, 20, 21, 24, 27, 30, 31 and so on) so the projection is a score that actually happens.

**Injury report and kickoff weather.** Quarterbacks at any status and anyone ruled out, plus an Open-Meteo forecast for outdoor stadiums at kickoff. Domes show as indoors and neutral-site games are skipped.

**Odds history.** Every run appends a price point per game to `history.json`, so each card shows where the line opened, where it is now, and a sparkline of the week. The scraper also prices next week's games, so a fresh week already has an opening line to compare against.

**Pick tracking.** Tap to swap any pick to the underdog, pick by hand for games with no odds, then log the week. Records fill in automatically once results land. The log lives on the phone and optionally syncs to your own Supabase project.

## Files

| File | What it is |
|---|---|
| `fetch_data.py` | Standard-library Python scraper. Writes `data.json` and `history.json`. |
| `index.html` | The whole front end: one file, Tailwind via CDN, no build step. |
| `.github/workflows/update_odds.yml` | Scheduled and manual runs, commits the data back to the repo. |
| `supabase_setup.sql` | Optional. Creates the pick-log table with row level security. |
| `data.json` | Current week: games, probabilities, picks, injuries, weather, tiebreaker. |
| `history.json` | Running odds log, pruned 28 days after kickoff. |

### Data sources

| Source | Used for | Key needed |
|---|---|---|
| ESPN scoreboard | Schedule, scores, injury report | No |
| The Odds API | Moneyline, spread, total | Yes, free tier |
| Kalshi | Prediction market prices | No |
| Polymarket (Gamma) | Prediction market prices | No |
| Action Network | Public ticket percentages | No, unofficial |
| Open-Meteo | Kickoff forecast | No |

## Setup

### 1. Repository

1. Fork or copy this repo. Put `fetch_data.py`, `index.html` and the workflow at the top level.
2. **Settings, then Pages:** deploy from your default branch, root folder.
3. **Settings, then Actions, then General, then Workflow permissions:** choose "Read and write." Without this the workflow can't commit data back.

Note that a GitHub Pages site is public even when the repo is private, unless you're on an Enterprise plan. Anyone with the link can see your picks.

### 2. API key

Get a free key from [The Odds API](https://the-odds-api.com/), then go to **Settings, then Secrets and variables, then Actions**, and add a repository secret named `ODDS_API_KEY`.

Without the key the app still works from prediction markets alone, but with less accuracy.

### 3. First run

Open the **Actions** tab, choose **Update odds and picks**, and click **Run workflow**. When it finishes, the site is live at `https://<username>.github.io/<repo>/`.

### 4. iPhone Home Screen

Open the site in Safari, tap Share, then **Add to Home Screen**. It runs full screen with no browser bars, and pull-to-refresh works inside the app.

### 5. Optional: sync the pick log

Without this, your log lives only in that browser's storage and is lost if you clear Safari or change phones.

1. Create a free project at [supabase.com](https://supabase.com). Under Security, leave "Enable Data API" on and turn off "Automatically expose new tables."
2. Run `supabase_setup.sql` in the SQL Editor. It creates `pick_log` with policies that limit each user to their own rows.
3. **Authentication, then Sign In / Providers, then Email:** turn off "Confirm email."
4. **Project Settings, then API:** copy the Project URL and the anon key. Never use the service role key.
5. In the app, scroll to Pick log, tap **Set up sync**, enter the URL, key, an email and a password, then tap **Create account**.
6. Go back to Supabase and turn off "Allow new users to sign up."

## Schedule

The workflow runs daily at 13:00 UTC (8 AM Central) and 01:00 UTC (8 PM Central), plus an extra run Thursday at 15:00 UTC for pools with Thursday deadlines. Edit the `cron` lines to suit your own deadline, and remember GitHub can start scheduled runs 30 or more minutes late. Manual runs from the GitHub mobile app take about a minute.

Each run costs one request against The Odds API quota, so roughly 15 to 20 a week.

## Using it on pick day

1. Open the app and check that "Updated" shows today.
2. Read the amber **Check before you submit** card, which lists only games with a quarterback question or a line that moved.
3. Decide your swaps. The header suggests how many based on your pool size: with a dozen people, zero or one; with twenty or more, one or two. Best candidates carry both the red **Upset value** and amber **Contrarian option** boxes.
4. Tap **Copy all picks**, paste into your pool form, then tap **Log picks**.

Swapping the Monday night game flips the projected score automatically so your tiebreaker agrees with your pick.

## Command-line options

```bash
export ODDS_API_KEY="your_key"
python3 fetch_data.py                      # current week, writes data.json + history.json
python3 fetch_data.py --week 5 --season 2026 --season-type 2
python3 fetch_data.py --book-weight 2      # weight sportsbooks double
python3 fetch_data.py --poly-weight 0      # turn off Polymarket
python3 fetch_data.py --lookahead 2        # price two future weeks into history
python3 fetch_data.py --debug-public       # write Action Network diagnostics
python3 -m http.server                     # then open http://localhost:8000
```

Python 3.9 or newer. No `pip install` needed. Opening `index.html` straight from disk won't work, because browsers refuse to read `data.json` from a file path.

## Troubleshooting

**ESPN returns 403.** ESPN blocks datacenter traffic at times. The scraper tries an alternate host, retries, and falls back to building the schedule from The Odds API with an estimated week number. The app shows a banner when that happens.

**No public betting splits.** Action Network's feed is unofficial and may be unavailable. Run the workflow with the **Show Action Network diagnostics** option, and the run summary will show what came back for each request.

**The app looks stale.** Check the build stamp at the very bottom of the page. GitHub Pages caches HTML for about ten minutes; add `?v=2` to the URL in Safari to force a fresh copy. An installed Home Screen app caches separately, so removing and re-adding it is the last resort, though that clears its stored swaps and log.

**Sparklines are missing.** Each game needs at least three history points, so they appear after a day or so of runs.

## Honest limits

- The markets already price injuries, weather and news. This tool surfaces that information; it does not beat the market, and it deliberately applies no adjustments of its own.
- Public ticket percentages come from sports bettors, not your coworkers. They're a decent proxy for what the room will pick, not a count of it.
- Contrarian swaps lower your expected number of correct picks. They're worth making to win a week outright in a big pool, not as a weekly habit.
- Action Network and Polymarket endpoints are unofficial and can change or block traffic without notice. Every source fails softly: the run still finishes and the app says which source was unavailable.

Built for personal use in a friendly office pool. Not betting advice.
