# Fabricatr forum lead monitor

A GitHub Actions cron job that watches metal-fab / welding / machine-shop forums and subreddits
for shop owners talking about how they run the business side of the shop (spreadsheets,
whiteboards, quoting, job tracking, ERP), and pings a Slack channel the moment a new one
appears so a human can go reply on the site.

**Read-only by design.** The script fetches RSS/Atom feeds and posts to one Slack incoming
webhook. It never posts, replies, votes, logs in, or scrapes HTML anywhere. There is no code
path that writes to a forum or to Reddit.

## How it works

```
every 15 min ──> fetch enabled feeds (config.yaml)
             ──> drop entries already in state/seen.json
             ──> Layer 1: keyword match on title + body (config.yaml → keywords)
             ──> Layer 2: OpenAI gpt-4.1-nano yes/no + one-sentence reason (cap: 40 calls/run)
             ──> YES → Slack webhook, one message per lead
             ──> commit state/seen.json back to the repo ([skip ci])
```

Slack message format:

```
🔩 Fab lead — Practical Machinist / Shop Management
*How do you guys keep track of jobs without losing your mind*
"we're running 12 jobs a week off a whiteboard and a spreadsheet and I've double-booked the brake twice..."
Why: shop owner describing job tracking pain, mentions spreadsheet + whiteboard
→ https://www.practicalmachinist.com/forum/threads/...
```

## Setup

1. Create a **public** GitHub repo and push this code (public = unlimited Actions minutes;
   a private repo on the Free plan runs out of minutes around day 20 at a 15-min cadence).
2. In the repo: Settings → Secrets and variables → Actions → New repository secret. Add:
   - `SLACK_WEBHOOK_URL` — Slack incoming webhook for the channel you want pinged
   - `OPENAI_API_KEY` — OpenAI API key used by the classifier
3. Actions → **monitor** → Run workflow with `probe = true` to confirm feeds are reachable
   from GitHub's runners.
4. The first real run is the **baseline**: it marks everything currently in the feeds as
   seen and sends nothing. Alerts start on the run after that.

Nothing secret lives in the repo. `state/seen.json` is just a map of feed entry IDs to the
last time they were seen.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | The whole thing, one script |
| `config.yaml` | Sources (with `tier` and `enabled` flags), keyword lists, limits, model |
| `state/seen.json` | Seen entry IDs; committed by the workflow after every run |
| `.github/workflows/monitor.yml` | Cron + manual-run workflow |
| `requirements.txt` | Pinned deps: feedparser, requests, PyYAML (OpenAI is called over plain HTTP) |

## Running locally

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
# or: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python monitor.py --probe                              # which feeds are alive
.venv/bin/python monitor.py --dry-run --ignore-seen --no-classify  # keyword layer only, prints hits
OPENAI_API_KEY=sk-... .venv/bin/python monitor.py --dry-run --ignore-seen      # full pipeline, Slack messages to stdout
# (or put OPENAI_API_KEY=... in a local .env file — it is git-ignored — and run: set -a; . ./.env; set +a)
```

| Flag | Effect |
|---|---|
| `--dry-run` | Print Slack messages to stdout instead of posting. Never writes `state/seen.json`. |
| `--no-classify` | Skip the OpenAI layer; every keyword hit counts as a lead. No API key needed. |
| `--probe` | Fetch every enabled feed and print alive / dead + item counts. Nothing else. |
| `--ignore-seen` | Treat every entry as new. Testing only; pair with `--dry-run`. |
| `--reset-baseline` | Forget all seen IDs and re-baseline (marks everything seen, sends nothing). |

All of these are also available as checkboxes on the manual **Run workflow** button in the Actions tab.

## Where things are

- **Logs**: Actions tab → the run → job `monitor` → step **Run monitor**. Every run prints
  one line per feed (`FEED OK` / `FEED ERROR`), one line per keyword hit (`KW HIT ... -> [words]`),
  one line per classifier verdict (`YES`/`NO` with the reason), any `SKIP` past the cost cap,
  `SLACK ERROR`s, and a final `SUMMARY` line. Readable from a phone.
- **State**: `state/seen.json` in the repo, committed by `github-actions[bot]` with `[skip ci]`.
- **Secrets**: GitHub Actions secrets only. Never in the repo.

## How to add a feed

Add a block under `sources:` in `config.yaml`:

```yaml
  - id: my_forum            # unique, used in logs
    name: My Forum          # shown in the Slack header
    tier: 1
    enabled: true
    url: https://example.com/forums/-/index.rss
    # optional — keep only items whose <category> matches these (XenForo node id → label):
    # category_filter:
    #   "12": Shop Talk
    # optional — wait this long before fetching (default: settings.delay_between_requests_s):
    # delay_before_s: 60
```

Then run `python monitor.py --probe` (or the workflow with `probe = true`) to confirm it
returns entries. Entries already in the feed when you enable it **will** be treated as new on
the next run unless you re-baseline, so expect a burst of hits from that source once.

Feed URL patterns that usually work:

- XenForo: `https://<site>/forums/<slug>.<id>/index.rss` (per forum) or `/forums/-/index.rss` (site-wide)
- vBulletin: `https://<site>/external.php?type=RSS2&forumids=<id>`
- Reddit: `https://www.reddit.com/r/<a>+<b>+<c>/new/.rss?limit=100`
- groups.io: `https://groups.io/g/<group>/rss` (only if archives are public)
- Google Alerts: create the alert with **Deliver to: RSS feed**, copy the feed URL

## How to add keywords

Edit `keywords:` in `config.yaml`. Three lists:

- `software` — product / category names. Any match passes Layer 1.
- `pain` — how owners describe the mess before they know the word "ERP". Any match passes.
- `paired_only` — words that only count when a `pain` word also matched (QuickBooks).

Matching is case-insensitive and whole-word (`E2` will not match `SE2000`; multi-word
phrases match as phrases with flexible whitespace). No code changes needed.

To tune: read the `NO ... — <reason>` lines in the run logs. If a keyword keeps producing
NOs for the same reason, drop it or make it more specific.

## How to change the schedule

Edit the cron line in `.github/workflows/monitor.yml`:

```yaml
    - cron: "*/15 * * * *"
```

Do not go below 15 minutes: Reddit rate-limits anonymous clients and GitHub often delays
scheduled runs by a few minutes anyway. To pause everything, disable the workflow in the
Actions tab (Actions → monitor → ⋯ → Disable workflow).

## How to reset the baseline

Use this when you have added a bunch of sources and want to skip the burst, or if state got
weird.

- Actions → monitor → Run workflow → tick **reset_baseline** → Run. That run marks everything
  currently in the feeds as seen, sends nothing, and commits the fresh state.
- Or: delete `state/seen.json`, commit, push. The next scheduled run re-baselines.

## Cost guard

`max_classifier_calls_per_run: 40` in `config.yaml`. Keyword hits beyond the cap are logged
as `SKIP` and left unseen so they get classified on the next run. Each call is roughly 700
input + 60 output tokens on gpt-4.1-nano ($0.10 / $0.40 per million), about a hundredth of a
cent. Worst case with the cap maxed on every run is well under $1/day; a normal day is
fractions of a cent. Change `settings.model` in `config.yaml` to use a different OpenAI chat model.

## Source status (verified 2026-09-14)

### Working

| Source | Feed | Notes |
|---|---|---|
| Practical Machinist | site-wide `/forum/forums/-/index.rss` | Filtered to Shop Management (49), Fabrication (30), CNC Machining (21), General (38). Per-subforum feeds are behind a Cloudflare challenge. |
| Reddit trade subs | one multireddit `/new/.rss` | Welding, Fabrication, metalworking, Machinists, Machining, CNC, sheetmetal, Blacksmith, Ironworkers, Millwrights, HVAC, PlasmaCutting |
| Reddit business subs | one multireddit `/new/.rss` | sweatystartup, Entrepreneur, manufacturing, ERP, Bluecollar |

### No feed — check manually

These are in `config.yaml` with `enabled: false` so the URL is on record. All of them sit
behind a bot challenge (Cloudflare "Just a moment", proof-of-work JS, or similar) that a
feed reader cannot pass. We do not scrape them.

| Site | What to look at | Why it's dead for us |
|---|---|---|
| WeldingWeb | Welding Business and Shop Talk; general welding discussion | Proof-of-work JS challenge in front of `external.php` |
| Shop Floor Talk | Business; main shop talk section | Cloudflare challenge |
| CNCZone (now **cncarena.com**) | Business Development / Shop Management; plasma & fabrication sections | Redirects to en.cncarena.com, Cloudflare challenge |
| FabricationForum.com | General fabrication discussion | Bot challenge wall |
| The Fabricator / FMA forum | Fabricating, welding, management categories | Cloudflare challenge |
| American Welding Society forum (app.aws.org/forum) | Shop Talk / Business | Cloudflare challenge |
| Eng-Tips | Structural steel fabrication forum | Cloudflare challenge |
| Steel-Detail (groups.io) | steel-detail@groups.io | See below |

**Steel-Detail on groups.io.** groups.io rate-limited the probe and the group page indicates
archives may be members-only. To try it: join at https://groups.io/g/steel-detail with your
email, then open the group's **Messages** page and look for the RSS icon / **Feed** link. If
archives are public, `https://groups.io/g/steel-detail/rss` should work: paste it into
`config.yaml`, set `enabled: true`, run the probe. If archives are members-only, groups.io
does not offer a token-authenticated feed, so it stays a manual check (or switch the
subscription to daily digest email).

### Google Alerts (Tier 3) — you create these

Signed in to your Google account at https://www.google.com/alerts, for each query below:
type the query, click **Show options**, set **Deliver to: RSS feed**, click **Create Alert**.
Back on the alerts page, click the RSS icon next to the alert and copy the URL
(`https://www.google.com/alerts/feeds/<number>/<number>`). Paste it into the matching
`galert_*` entry in `config.yaml` and set `enabled: true`.

1. `"fabrication shop software"`
2. `"fab shop" software`
3. `"welding shop" software OR spreadsheet`
4. `"steel fabrication" ERP`
5. `"job shop" ERP OR software`
6. `"shop management software" welding OR fabrication`
7. `JobBoss OR "E2 Shop System" OR ProShop OR Fulcrum OR "Global Shop"`

These are low volume and the classifier kills most of it. The script unwraps Google's
redirect links so the Slack link goes straight to the page.

### Not feasible for free — check manually

- **Facebook groups.** No feed; scraping violates Facebook's ToS and gets accounts banned.
  Not attempted. Highest-volume place owners complain about running a shop, so check daily:
  - Metal Fabrication Shop Owners
  - Welding Business Owners
  - Machine Shop Owners
  - (and similar: "CNC Machine Shop Owners", "Fab Shop Owners", "Welding Business", "Small Machine Shop Owners")
- **LinkedIn groups** (manufacturing ops / ERP groups). No feed, no scraping.
- **r/Metalworking + r/Machining Discord.** Could be done later with a bot invited to the
  server. Not built.
- **X/Twitter.** No free API. Skipped.

## Dedup and state details

- Entry ID is the feed's `<id>`/`<guid>`, falling back to the link.
- Seen IDs are refreshed every time they appear in a feed and pruned after
  `seen_retention_days` (30) of not appearing, so `seen.json` stays small.
- Old threads that resurface because someone replied (common on Practical Machinist's
  site-wide feed) are ignored if their published date is older than `max_entry_age_hours` (72).
- The workflow uses a concurrency group so two runs never race on `seen.json`.
