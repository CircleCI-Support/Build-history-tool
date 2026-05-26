#!/usr/bin/env python3
"""
Export recent CircleCI pipeline (and optional workflow/job) history to CSV via API v2.

Authentication: set CIRCLE_TOKEN to a personal API token (User Settings → Personal API Tokens).
Exports one project only: pass -p/--project or set CIRCLECI_PROJECT_SLUG (e.g. gh/myorg/myrepo).

Examples:
  export CIRCLE_TOKEN=xxxxx
  export CIRCLECI_PROJECT_SLUG=gh/myorg/myrepo
  ./circleci_build_history_export.py --interval weekly -o builds.csv
  ./circleci_build_history_export.py -p gh/myorg/myrepo --interval weekly -o builds.csv
  ./circleci_build_history_export.py -p gh/myorg/myrepo --interval daily --include-workflows

Generate a crontab line (no token needed for this step):
  ./circleci_build_history_export.py --print-cron -p gh/myorg/myrepo -i weekly -o /abs/path/weekly.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Any

from circleci_export_utils import (
    ENV_PROJECT_SLUG,
    INTERVAL_DAYS,
    emit_crontab_snippet,
    iter_pipeline_workflows,
    iter_project_pipelines,
    iter_workflow_jobs,
    job_row,
    pipeline_base_row,
    resolve_project_slug,
    verify_project_exists,
    window_from_args,
    workflow_row,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export CircleCI build history (API v2) to CSV.",
    )
    parser.add_argument(
        "-p",
        "--project",
        default=None,
        metavar="SLUG",
        help=f"Project slug, e.g. gh/myorg/myrepo (default: env {ENV_PROJECT_SLUG})",
    )
    parser.add_argument(
        "--verify-project",
        action="store_true",
        help="Call the API to confirm the project exists before fetching pipelines",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="circleci_build_history.csv",
        help="Output CSV path (default: circleci_build_history.csv)",
    )
    parser.add_argument(
        "-i",
        "--interval",
        choices=list(INTERVAL_DAYS.keys()),
        default="weekly",
        help="How far back to capture: daily, weekly, monthly, quarterly, yearly (default: weekly)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="Override --interval with an explicit number of days lookback",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="ISO8601",
        help="Start of window (ISO-8601). Overrides --interval / --days if set.",
    )
    parser.add_argument(
        "--until",
        default=None,
        metavar="ISO8601",
        help="End of window (ISO-8601, default: now UTC)",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Only pipelines for this branch (passed to the pipelines API)",
    )
    parser.add_argument(
        "--mine",
        action="store_true",
        help="Only pipelines triggered by the token's user",
    )
    parser.add_argument(
        "--include-workflows",
        action="store_true",
        help="One CSV row per workflow (extra API calls per pipeline)",
    )
    parser.add_argument(
        "--include-jobs",
        action="store_true",
        help="One CSV row per job (implies workflow columns; many API calls)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="CircleCI personal API token (default: env CIRCLE_TOKEN)",
    )
    parser.add_argument(
        "--print-cron",
        action="store_true",
        help=(
            "Print a crontab line and setup hints for this invocation, then exit "
            "(no API calls; does not require CIRCLE_TOKEN). Combine with -p/-i/-o etc."
        ),
    )
    parser.add_argument(
        "--cron-schedule",
        default=None,
        metavar="EXPR",
        help=(
            "Cron schedule (five fields: min hour dom mon dow) used only with --print-cron. "
            "Default matches -i (e.g. weekly → Monday 09:00)."
        ),
    )
    parser.add_argument(
        "--cron-workdir",
        default=None,
        metavar="DIR",
        help=(
            "Working directory for the printed crontab line (cd … &&). "
            "Default: directory containing this script."
        ),
    )
    parser.add_argument(
        "--cron-log",
        default=None,
        metavar="PATH",
        help=(
            "Log file for stdout/stderr in the printed crontab line (>> … 2>&1). "
            "Default: <cron-workdir>/circleci_export_cron.log"
        ),
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="On success, print nothing to stdout (stderr still used for errors)",
    )
    args = parser.parse_args()

    if args.print_cron:
        emit_crontab_snippet(args, script_path=__file__, argv=sys.argv[1:])
        return

    token = args.token or os.environ.get("CIRCLE_TOKEN")
    if not token:
        print("Set CIRCLE_TOKEN or pass --token.", file=sys.stderr)
        sys.exit(2)

    project_slug = resolve_project_slug(args.project)
    if args.verify_project:
        verify_project_exists(project_slug, token)

    if args.include_jobs:
        args.include_workflows = True

    window = window_from_args(args.interval, args.days, args.since, args.until)

    rows: list[dict[str, Any]] = []
    for p in iter_project_pipelines(
        project_slug,
        token,
        args.branch,
        args.mine,
        window,
    ):
        base = pipeline_base_row(p)
        if not args.include_workflows:
            rows.append(base)
            continue
        pid = p.get("id")
        if not pid:
            rows.append(base)
            continue
        wf_list = list(iter_pipeline_workflows(pid, token))
        if not wf_list:
            rows.append(base)
            continue
        for w in wf_list:
            wr = workflow_row(base, w)
            if args.include_jobs:
                wid = w.get("id")
                if not wid:
                    rows.append(wr)
                    continue
                jobs = list(iter_workflow_jobs(wid, token))
                if not jobs:
                    rows.append(wr)
                    continue
                for j in jobs:
                    rows.append(job_row(wr, j))
            else:
                rows.append(wr)

    if not rows:
        print("No pipelines in the selected time window.", file=sys.stderr)
        sys.exit(1)

    fieldnames = list(rows[0].keys())
    for r in rows[1:]:
        for k in r:
            if k not in fieldnames:
                fieldnames.append(k)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    if not args.quiet:
        print(f"Wrote {len(rows)} row(s) to {args.output}")


if __name__ == "__main__":
    main()
