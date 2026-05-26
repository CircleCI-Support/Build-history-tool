"""Shared helpers for CircleCI API v2 export (time windows, HTTP, rows, crontab snippet)."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_BASE = "https://circleci.com/api/v2"
ENV_PROJECT_SLUG = "CIRCLECI_PROJECT_SLUG"


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


INTERVAL_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
    "yearly": 365,
}

# Suggested cron schedule when --print-cron is used without --cron-schedule (minute hour dom mon dow).
CRON_SCHEDULE_BY_INTERVAL = {
    "daily": "5 9 * * *",
    "weekly": "0 9 * * 1",
    "monthly": "15 9 1 * *",
    "quarterly": "30 9 1 1,4,7,10 *",
    "yearly": "0 9 1 1 *",
}

# argv[1:] keys removed from the suggested crontab command (meta + secrets).
_CRON_STRIP_FLAGS = frozenset({"--print-cron"})
_CRON_STRIP_ONE_ARG = frozenset(
    {
        "--cron-schedule",
        "--cron-workdir",
        "--cron-log",
        "--token",
    }
)


def parse_iso_z(s: str | None) -> datetime | None:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def window_from_args(
    interval: str,
    days: int | None,
    since: str | None,
    until: str | None,
) -> Window:
    end = parse_iso_z(until) if until else datetime.now(timezone.utc)
    if since:
        start = parse_iso_z(since)
        if start is None:
            raise SystemExit("--since must be a valid ISO-8601 datetime")
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if start > end:
            raise SystemExit("--since must be before --until (or now)")
        return Window(start=start, end=end)

    n = days if days is not None else INTERVAL_DAYS[interval]
    start = end - timedelta(days=n)
    return Window(start=start, end=end)


def encode_project_slug(slug: str) -> str:
    return quote(slug, safe="")


def resolve_project_slug(cli_value: str | None) -> str:
    raw = (cli_value or os.environ.get(ENV_PROJECT_SLUG) or "").strip()
    if not raw:
        raise SystemExit(
            f"Project slug required: pass --project SLUG or set {ENV_PROJECT_SLUG} "
            "(e.g. export CIRCLECI_PROJECT_SLUG=gh/myorg/myrepo)."
        )
    return raw


def verify_project_exists(project_slug: str, token: str) -> None:
    enc = encode_project_slug(project_slug)
    api_request(f"/project/{enc}", token, None)


def api_request(path: str, token: str, params: dict[str, str | None] | None = None) -> dict[str, Any]:
    q = {k: v for k, v in (params or {}).items() if v is not None}
    url = f"{API_BASE}{path}"
    if q:
        url = f"{url}?{urlencode(q)}"
    req = Request(
        url,
        headers={
            "Circle-Token": token,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code} {e.reason} for {url}\n{err_body}") from e
    except URLError as e:
        raise SystemExit(f"Request failed: {e}") from e


def iter_project_pipelines(
    project_slug: str,
    token: str,
    branch: str | None,
    mine: bool,
    window: Window,
) -> Iterator[dict[str, Any]]:
    """Yield pipelines for this project only, newest first, within the time window."""
    enc = encode_project_slug(project_slug)
    base = f"/project/{enc}/pipeline"
    if mine:
        base = f"{base}/mine"

    page_token: str | None = None
    while True:
        data = api_request(
            base,
            token,
            {"branch": branch, "page-token": page_token},
        )
        items = data.get("items") or []
        next_token = data.get("next_page_token") or None

        for p in items:
            ps = p.get("project_slug")
            if ps is not None and ps != project_slug:
                continue
            created = parse_iso_z(p.get("created_at"))
            if created is None:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created > window.end:
                continue
            if created < window.start:
                # Pipelines are returned newest-first; older pages need not be fetched.
                return
            yield p

        if not next_token:
            break
        page_token = next_token
        time.sleep(0.15)


def iter_pipeline_workflows(pipeline_id: str, token: str) -> Iterator[dict[str, Any]]:
    page_token: str | None = None
    while True:
        data = api_request(
            f"/pipeline/{pipeline_id}/workflow",
            token,
            {"page-token": page_token},
        )
        for w in data.get("items") or []:
            yield w
        next_token = data.get("next_page_token") or None
        if not next_token:
            break
        page_token = next_token
        time.sleep(0.1)


def iter_workflow_jobs(workflow_id: str, token: str) -> Iterator[dict[str, Any]]:
    page_token: str | None = None
    while True:
        data = api_request(
            f"/workflow/{workflow_id}/job",
            token,
            {"page-token": page_token},
        )
        for j in data.get("items") or []:
            yield j
        next_token = data.get("next_page_token") or None
        if not next_token:
            break
        page_token = next_token
        time.sleep(0.1)


def pipeline_base_row(p: dict[str, Any]) -> dict[str, Any]:
    vcs = p.get("vcs") or {}
    commit = vcs.get("commit") or {}
    trig = p.get("trigger") or {}
    errs = p.get("errors") or []
    err_summary = "; ".join(
        f"{e.get('type', '')}:{e.get('message', '')}" for e in errs if isinstance(e, dict)
    )
    return {
        "pipeline_id": p.get("id"),
        "pipeline_number": p.get("number"),
        "project_slug": p.get("project_slug"),
        "state": p.get("state"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "branch": vcs.get("branch"),
        "revision": vcs.get("revision"),
        "commit_subject": commit.get("subject"),
        "trigger_type": trig.get("type"),
        "errors": err_summary,
    }


def workflow_row(base: dict[str, Any], w: dict[str, Any]) -> dict[str, Any]:
    row = dict(base)
    row.update(
        {
            "workflow_id": w.get("id"),
            "workflow_name": w.get("name"),
            "workflow_status": w.get("status"),
            "workflow_created_at": w.get("created_at"),
            "workflow_stopped_at": w.get("stopped_at"),
        }
    )
    return row


def job_row(wf_row: dict[str, Any], j: dict[str, Any]) -> dict[str, Any]:
    row = dict(wf_row)
    row.update(
        {
            "job_id": j.get("id"),
            "job_number": j.get("job_number"),
            "job_name": j.get("name"),
            "job_status": j.get("status"),
            "job_type": j.get("type"),
            "job_started_at": j.get("started_at"),
            "job_stopped_at": j.get("stopped_at"),
        }
    )
    return row


def filter_argv_for_crontab(argv: list[str]) -> list[str]:
    """Remove cron-helper flags and secrets so the printed command is safe to share."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _CRON_STRIP_FLAGS:
            i += 1
            continue
        if a in _CRON_STRIP_ONE_ARG:
            i += 2 if i + 1 < len(argv) else 1
            continue
        if a.startswith("--token="):
            i += 1
            continue
        if "=" in a:
            name = a.split("=", 1)[0]
            if name in _CRON_STRIP_ONE_ARG:
                i += 1
                continue
        out.append(a)
        i += 1
    return out


def emit_crontab_snippet(args: argparse.Namespace, *, script_path: str, argv: list[str]) -> None:
    schedule = args.cron_schedule or CRON_SCHEDULE_BY_INTERVAL.get(
        args.interval, "0 9 * * 1"
    )
    workdir = os.path.abspath(
        args.cron_workdir or os.path.dirname(os.path.abspath(script_path))
    )
    script_abs = os.path.abspath(script_path)
    py = sys.executable or "python3"
    inner_argv = filter_argv_for_crontab(argv)
    if "--quiet" not in inner_argv and "-q" not in inner_argv:
        inner_argv.append("--quiet")
    inner = shlex.join([py, script_abs] + inner_argv)
    env_file = os.path.expanduser("~/.circleci-cron.env")
    log_default = os.path.join(workdir, "circleci_export_cron.log")
    log_file = os.path.abspath(os.path.expanduser(args.cron_log or log_default))
    line = (
        f"{schedule} cd {shlex.quote(workdir)} && "
        f". {shlex.quote(env_file)} && "
        f"{inner} >> {shlex.quote(log_file)} 2>&1"
    )
    print(
        "# --- CircleCI build export (paste into: crontab -e) ---\n"
        "# Cron does not load your login shell; put secrets in a root-only file, e.g.:\n"
        f"#   printf '%s\\n' 'export CIRCLE_TOKEN=PASTE_TOKEN' "
        f"'export {ENV_PROJECT_SLUG}=gh/org/repo' > {shlex.quote(env_file)} && chmod 600 {shlex.quote(env_file)}\n"
        "# Use absolute paths for -o. Then add one line:\n"
        "#\n"
        f"{line}\n"
    )
