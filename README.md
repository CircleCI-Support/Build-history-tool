# CircleCI build history export

`circleci_build_history_export.py` pulls pipeline history from the [CircleCI API v2](https://circleci.com/docs/api/v2/) and writes it to a CSV file. Each run targets **one project** (by slug). You can narrow the time range (daily, weekly, custom dates, and so on) and optionally include workflows or individual jobs.

## Requirements

- **Python 3.9+** (uses the standard library only; no `pip install` needed)
- A **personal API token** with access to the project (see below)

Keep `circleci_build_history_export.py` and `circleci_export_utils.py` in the **same directory** when you run the script so Python can import the helpers.

| File | Role |
|------|------|
| `circleci_build_history_export.py` | Command-line entrypoint (argparse, CSV output) |
| `circleci_export_utils.py` | API client, time windows, row builders, `--print-cron` helpers |

## Authentication

CircleCI API v2 expects a **personal** token, not a project token.

1. Sign in to CircleCI.
2. Open **User Settings** → **Personal API Tokens**.
3. Create a token and copy it.

Set it in your environment (recommended):

```bash
export CIRCLE_TOKEN='your-token-here'
```

Or pass it once:

```bash
./circleci_build_history_export.py --token 'your-token-here' -p gh/org/repo -o out.csv
```

Avoid committing the token or putting it in shell history unnecessarily; prefer `export` in an interactive session or a secrets manager for cron.

## Project slug (required for each run)

History is always scoped to **a single project**. Use the same slug CircleCI shows in URLs:

| VCS        | Example slug              |
|-----------|---------------------------|
| GitHub    | `gh/myorg/myrepo`         |
| Bitbucket | `bb/my-workspace/myrepo` |

You can pass the slug on the command line or set it once in the environment:

```bash
export CIRCLECI_PROJECT_SLUG='gh/myorg/myrepo'
```

If both are set, `--project` wins. If neither is set, the script exits with an error.

Optional **`--verify-project`** performs a quick API check that the project exists (and that your token can access it) before downloading pipelines.

## Basic usage

```bash
chmod +x circleci_build_history_export.py   # once

export CIRCLE_TOKEN='...'
./circleci_build_history_export.py -p gh/myorg/myrepo -o builds.csv

# Same, with default project from the environment
export CIRCLECI_PROJECT_SLUG='gh/myorg/myrepo'
./circleci_build_history_export.py -o builds.csv

# Fail fast if the slug is wrong or inaccessible
./circleci_build_history_export.py -p gh/myorg/myrepo --verify-project -o builds.csv
```

Default time range is **weekly** (last 7 days). Default output file is `circleci_build_history.csv`.

### Time range

| Option | Description |
|--------|-------------|
| `-i` / `--interval` | `daily` (1 day), `weekly` (7), `monthly` (30), `quarterly` (90), `yearly` (365). Default: `weekly`. |
| `--days N` | Custom lookback: last **N** days from now. Overrides `--interval`. |
| `--since` | Start of window (ISO-8601). Using this overrides `--interval` and `--days`. |
| `--until` | End of window (ISO-8601). Default: current time (UTC). |

Examples:

```bash
# Last day
./circleci_build_history_export.py -p gh/myorg/myrepo -i daily -o last_day.csv

# Last two weeks
./circleci_build_history_export.py -p gh/myorg/myrepo --days 14 -o two_weeks.csv

# Fixed range (UTC)
./circleci_build_history_export.py -p gh/myorg/myrepo \
  --since 2026-03-01T00:00:00Z --until 2026-03-15T23:59:59Z -o sprint.csv
```

### Filters and detail level

| Option | Description |
|--------|-------------|
| `--branch NAME` | Only pipelines for that branch (passed to the API). |
| `--mine` | Only pipelines triggered by the token’s user. |
| `--include-workflows` | One CSV row per **workflow** (extra requests per pipeline). |
| `--include-jobs` | One CSV row per **job** (includes workflow columns; many more requests). |

Example with workflows:

```bash
./circleci_build_history_export.py -p gh/myorg/myrepo -i weekly \
  --include-workflows -o workflows.csv
```

## What’s in the CSV

Without extra flags, each row is roughly one **pipeline**, with fields such as pipeline id/number, project slug, state, timestamps, branch, revision, commit subject, trigger type, and errors.

With `--include-workflows`, rows add workflow id, name, status, and timestamps. With `--include-jobs`, rows also include job id, number, name, status, type, and start/stop times.

Exact columns depend on what CircleCI returns for your project.

## Scheduling (cron)

Cron does **not** load your interactive shell, so `export CIRCLE_TOKEN=...` in `~/.zshrc` is **not** applied to scheduled jobs. Prefer a small env file that you `source` from the crontab line (see `--print-cron` below).

### Generate a crontab line (`--print-cron`)

Use the same flags you would use for a normal export (`-p`, `-i`, `-o`, `--include-workflows`, etc.). The script prints a ready-to-paste **crontab** line plus comments for creating `~/.circleci-cron.env`. **No API token is required** for this step.

```bash
./circleci_build_history_export.py --print-cron \
  -p gh/myorg/myrepo \
  -i weekly \
  -o /absolute/path/to/weekly_builds.csv
```

Optional tuning (only affects the printed line):

| Flag | Purpose |
|------|--------|
| `--cron-schedule EXPR` | Five cron fields (`min hour dom mon dow`). If omitted, a default is chosen from `-i` (e.g. weekly → Monday 09:00). |
| `--cron-workdir DIR` | Directory for `cd … &&` before running the script (default: directory containing the script). |
| `--cron-log PATH` | File used for `>> … 2>&1` in the snippet (default: `<cron-workdir>/circleci_export_cron.log`). |

Then:

1. Create `~/.circleci-cron.env` with `export CIRCLE_TOKEN=…` and `export CIRCLECI_PROJECT_SLUG=…` (if you do not pass `-p` on the command line), and `chmod 600` that file.
2. Run `crontab -e` and paste the printed line.
3. Prefer **absolute paths** for `-o` and `--cron-log`.

### Quiet runs from cron (`-q` / `--quiet`)

To avoid printing the usual “Wrote *N* row(s)…” line on success (cron may email stdout), add `--quiet`. The generated `--print-cron` snippet appends this automatically.

### Manual example (without `--print-cron`)

```cron
0 9 * * 1 cd /absolute/path/to/this/folder && . "$HOME/.circleci-cron.env" && ./circleci_build_history_export.py -p gh/myorg/myrepo -i weekly -o /absolute/path/weekly_builds.csv --quiet >> /absolute/path/cron.log 2>&1
```

## Troubleshooting

- **`Set CIRCLE_TOKEN or pass --token`** — Export the token or pass `--token`.
- **HTTP 401 / 403** — Token invalid, expired, or missing access to the organization/project.
- **HTTP 404** — Wrong project slug or project not under this CircleCI account.
- **`No pipelines in the selected time window`** — No runs in that range; widen `--interval`, `--days`, or the `--since` / `--until` range.

For full options:

```bash
./circleci_build_history_export.py --help
```
