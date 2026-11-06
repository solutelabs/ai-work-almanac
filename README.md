# AI work almanac (Pulse)

Pulse shows how a team works with AI coding tools: who uses which tools, how much, and what comes out of it in commits and pull requests. The dashboard reads like a wall almanac: one big numeral per day, tinted by how much AI work happened, with the detail packed around it.

It's built for leads and project managers who want to understand how work happens, not to rank people. It says "no data" where someone hasn't installed the collector, because no data isn't the same as no AI use.

Built at SoluteLabs.

| Part | What it does |
|---|---|
| `collector/collect.py` | Runs hourly on each developer's laptop and sends counts from Claude Code and Codex logs. One Python 3.9+ file, standard library only |
| `tracker.py` | Reads your GitHub org's repos through the API and uploads commits, PRs and, optionally, logged time from solidtime |
| `worker/` | Cloudflare Worker: receives the data, serves the dashboard and the collector, and posts a weekly digest |
| `worker/migrations/` | Postgres tables, in a `tracker` schema |

## Try the demo

```bash
cd worker && npm ci && npm run demo
```

This builds `worker/dev/demo/dist/`: the real dashboard page, answered by made-up data in the browser, with no server or database. Open its `index.html`. The build refuses to finish if the page names anyone in `people.json` or `solidtime_projects.json`.

## What the collector sends

**Usage counts:** tool and version, a hash of the log file's path, session ID, day, model, `org/repo`, lines added and removed, token counts, how many prompts were typed, which 5-minute blocks of each day had AI activity or a prompt, and a hash of each working folder with whether it's a git worktree. Also the machine name, OS, collector version and run health counts. Sessions with no repo count under "No repo", with no folder name sent.

**The developer's own commits:** from repos on the laptop, on any git host: `owner/repo`, commit ID, time, line counts, whether it names an AI tool, and whether it has reached the default branch. Commit messages and code aren't sent.

**Whether a team harness is set up:** for repos the developer worked in, whether your team's Claude Code plugin (`HARNESS_PLUGIN` in `collect.py`) is on, its version, and whether git runs its hooks. Settings and hook contents aren't sent.

**Prompts, responses and tool use:** what the developer types, the AI's text responses, and the commands and file paths its tools used, usually the last 30 days.

## Privacy design

- The install shows a notice (`INSTALL_NOTICE` in `collect.py`) that says exactly what is sent. If a new version changes the notice, laptops keep the old version until the person re-installs, so what's collected can't grow without them seeing it. Edit the notice to name who in your organisation reads prompts.
- File contents and command output are never sent, though text someone wrote may quote them.
- Known secret formats are replaced on the laptop before upload. Only the type and a fingerprint are reported, so the key can be rotated. Secrets with no recognisable format can slip through.
- Prompt text goes to a private R2 bucket, separate from the dashboard database. Set a 90-day lifecycle rule on it.
- Only the emails in `PROMPT_READERS` can read prompt text on the dashboard, and every read is logged in the bucket under `access-log/`.
- Anyone can see what would be sent without sending it:

```bash
python3 collect.py run --dry-run         # usage counts
python3 collect.py capture-preview       # prompts, responses and tool use, written to a local file
```

## Self-host

You need a Cloudflare account, a Postgres database and a GitHub org.

### 1. Database

Apply the migrations in order. They create a `tracker` schema and a `tracker_app` role that can reach nothing outside it:

```bash
for f in worker/migrations/*.sql; do psql "$DATABASE_URL" -f "$f"; done
```

Then give the role a login with a password you generate: `ALTER ROLE tracker_app LOGIN PASSWORD '...';`

### 2. Worker

Fill in the placeholders in `worker/wrangler.jsonc`. Each one has a comment saying what goes there. Create a Hyperdrive config, which gives the Worker its database connection, and put its ID in `wrangler.jsonc`:

```bash
cd worker
npm ci
npx wrangler login
npx wrangler hyperdrive create pulse-tracker --origin-connection-limit 5 \
  --connection-string="postgres://tracker_app:PASSWORD@your-db-host:5432/postgres"
npx wrangler r2 bucket create pulse-tracker-capture
npx wrangler deploy
npx wrangler secret put ADMIN_TOKEN
npx wrangler secret put GIT_UPLOAD_TOKEN   # fine-grained GitHub token: this repo only, Actions read and write
```

Set `UPLOAD_REPO` in `wrangler.jsonc` to your copy of this repo (`owner/name`). The Worker's cron starts the git upload there, and the weekly digest ignores activity in it.

### 3. GitHub sign-in for laptops

In your org's settings, under Developer settings, create an OAuth App (any homepage and callback URL work) and tick **Enable Device Flow**. No client secret is needed. Put its client ID in `GITHUB_CLIENT_ID` and your org in `ALLOWED_ORGS`, then deploy again. Members of those orgs can now enroll a laptop themselves.

### 4. Dashboard sign-in

Until `ACCESS_AUD` is set, the dashboard asks for a password: set one with `npx wrangler secret put DASHBOARD_PASSWORD`.

To use Google sign-in through Cloudflare Access instead:

1. In Google Cloud, create an OAuth client of type **Web application**, with the redirect URI `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`.
2. In Cloudflare Zero Trust, under **Settings > Authentication**, add **Google** as a login method with that client's ID and secret.
3. Under **Access > Applications**, add a self-hosted application for exactly `/dashboard` and `/api/dashboard` on your host. Don't protect the whole hostname, because laptops install and upload through it. Add an **Allow** policy with the permitted emails.
4. Put the application's **Audience (AUD) tag** in `ACCESS_AUD` and the team name in `ACCESS_TEAM`, then deploy. The password stops working, and any request without a valid Access token gets a 403.

### 5. Git upload

`.github/workflows/nightly.yml` uploads commits and PRs. The Worker's cron starts it every 15 minutes. In the repo's settings, add the secrets `TRACKER_GITHUB_TOKEN` (read access to the org's repos and PRs) and `TRACKER_ADMIN_TOKEN` (the Worker's `ADMIN_TOKEN`), and the variables `TRACKER_URL` (your Worker's address) and `TRACKER_ORG` (your GitHub org).

To show logged hours, also set `SOLIDTIME_TOKEN` and, if your solidtime server isn't the default, `SOLIDTIME_API`. `solidtime_projects.json` lists which repos each solidtime project covers. Billable rates and descriptions are never stored.

`people.json` maps each person's emails and GitHub logins to one display name. The example files hold made-up people and projects: replace them with your own.

### 6. Install the collector

On each laptop, with Python 3.9+, git and a GitHub account in your org:

```bash
curl -fsSL https://pulse.example.com/collect.py -o /tmp/collect.py && python3 /tmp/collect.py install --url https://pulse.example.com
```

On Windows (PowerShell):

```powershell
irm https://pulse.example.com/collect.py -OutFile $env:TEMP\collect.py; py $env:TEMP\collect.py install --url https://pulse.example.com
```

Or set `DEFAULT_URL` in `collect.py` to your host and drop `--url`. The install opens GitHub to confirm who the person is, then sets up an hourly job (launchd on Mac, a systemd timer or cron on Linux, Task Scheduler on Windows). The GitHub sign-in isn't saved. Each laptop gets its own tracker token.

The collector updates itself from the Worker on its hourly run. Bump `VERSION` in `collect.py` to ship a new one.

Check it with `python3 ~/.pulse-tracker/collect.py status`. Remove it with `python3 ~/.pulse-tracker/collect.py uninstall`.

### 7. Settings kept in code

Everything else that's specific to your team is a constant you edit before deploying:

| Setting | Where | What it does |
|---|---|---|
| `INSTALL_NOTICE` | `collector/collect.py` | What laptops are told is collected. Name who reads prompts |
| `DEFAULT_URL` | `collector/collect.py` | Your Worker's address, so installs don't need `--url` |
| `HARNESS_PLUGIN` | `collector/collect.py` | Your team's Claude Code plugin, as `plugin@marketplace`, if you have one |
| `LEFT_OUT` | `worker/src/digest.js` | People the weekly digest never names |
| `REPO_RENAMES` | `worker/src/repo_renames.js` | Old repo names to merge into current ones |
| `people.json`, `solidtime_projects.json` | repo root | Who is who, and which repos each solidtime project covers |

Days and times are in India Standard Time (IST, UTC+5:30): the dashboard, the digest's Monday 09:30 send and the collector's day boundaries all use it.

## Other admin tasks

Revoke a laptop's access when someone leaves the org:

```bash
curl -X POST https://pulse.example.com/api/admin/developers/revoke \
  -H "Authorization: Bearer $ADMIN_TOKEN" -d '{"email": "asha.rao@example.com"}'
```

**Weekly digest:** every Monday the Worker can post a summary to Slack: days with activity but nothing logged, logged days with no activity, and anyone not sending data. Create an incoming webhook and run `npx wrangler secret put SLACK_DIGEST_WEBHOOK`. The people and repos it leaves out are at the top of `worker/src/digest.js`. It holds names and counts, so send it somewhere private.

**Leave:** send leave days from your HR system so the dashboard doesn't treat them as gaps. Each send replaces that date range.

```bash
curl -X POST https://pulse.example.com/api/admin/leave \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"from":"2026-10-01","to":"2026-10-31","days":[{"email":"asha.rao@example.com","day":"2026-10-14"},{"email":"asha.rao@example.com","day":"2026-10-15","portion":0.5}]}'
```

**Renamed repos:** add the old name to `worker/src/repo_renames.js` and deploy, so the dashboard merges both names.

**Read captured text,** for whoever holds `ADMIN_TOKEN`. Every read is logged.

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" "https://pulse.example.com/api/admin/capture/list?day=2026-11-05"
curl -H "Authorization: Bearer $ADMIN_TOKEN" "https://pulse.example.com/api/admin/capture/findings?days=7"
```

## Run it locally

With Postgres running on your machine:

```bash
cd worker && npm ci
npm run db:reset     # builds a local pulse_dev database from the migrations and made-up data
npm run dev          # http://localhost:8787/dashboard, any username, password: local
```

A local run uses a local database and a local R2 bucket. Google sign-in is off and the cron jobs don't run.

## Run the tests

```bash
(cd worker && npm test)                                    # Worker, including the collector contract
(cd collector && python3 -m unittest -b test_collect test_stress)
python3 -m unittest -b test_tracker                        # git upload
(cd scripts && python3 -m unittest -b test_live_check test_weekly_status)
```

`scripts/live_check.py` checks a deployed site and keeps one GitHub issue open while it fails. `scripts/weekly_status.py` posts a weekly summary of releases and open PRs in this repo to Slack.

## Credits

Built at SoluteLabs. The dashboard's fonts in `worker/src/fonts/` are Archivo and Archivo Narrow, from Google Fonts under the SIL Open Font License. Released under the MIT licence: see `LICENSE`.
