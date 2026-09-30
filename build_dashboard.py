#!/usr/bin/env python3
"""
Build a static HTML dashboard from a GitHub Project (Projects v2).

Usage:
    python build_dashboard.py                  # live data (needs PROJECT_TOKEN env var)
    python build_dashboard.py --demo           # made-up data, to preview the layout
    python build_dashboard.py --out my.html    # output path (default: site/index.html)

The page only shows aggregate numbers (counts, points, hours per project/day/week).
Issue titles are never fetched or written to the page.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
from plotly.subplots import make_subplots

# ======================================================================
# SETTINGS: edit this block
# ======================================================================

# Who owns the "Research Tasks" project: "user" for a personal account,
# "organization" for an organisation.
OWNER_TYPE = "user"
OWNER_LOGIN = "YOUR-GITHUB-USERNAME"
# The number at the end of the project's URL, e.g.
#   https://github.com/users/YOUR-GITHUB-USERNAME/projects/3   ->   3
PROJECT_NUMBER = 0

# Field names, exactly as they appear in the project.
FIELD_STATUS = "Status"
FIELD_POINTS = "Points"
FIELD_DREAD = "Dread Bump"
FIELD_DEADLINE = "Deadline"
FIELD_PROJECT = "Project"   # research projects are detected automatically from this field

# Status options.
STATUS_DONE = "Done"
# Statuses that count as "upcoming" work. Remove "Paused / Background" if you'd
# rather keep paused tasks out of the forward-looking numbers.
UPCOMING_STATUSES = ["Todo", "In Progress", "Paused / Background"]

# Dropdown label -> number. (Labels not listed here are read as numbers if
# possible, e.g. a new "6" option, and flagged at the bottom of the page.)
POINTS_MAP = {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
DREAD_MAP = {"+0": 0, "+1": 1, "+2": 2}

# Base points -> expected duration range in hours (low, high).
# Headline "expected time" uses the midpoint of each range; dread doesn't add time.
HOURS_RANGE = {1: (0, 1), 2: (1, 2), 3: (2, 3), 4: (3, 4), 5: (4, 5)}

# How the completion date of a Done task is worked out:
#   "status": when the Status field was last changed (i.e. set to Done),
#             falling back to the issue's closed date if that's missing
#   "closed": the issue's closed date, falling back to the Status change
COMPLETION_DATE_SOURCE = "status"

TIMEZONE = "Europe/London"   # decides which calendar day/week a task counts towards
DEADLINE_WINDOW_DAYS = 14    # "due soon" window on the headline cards
PACE_WEEKS = 4               # full weeks averaged for the "recent pace" card
NO_PROJECT_LABEL = "(no project)"

# Optional: fix the order (and therefore colours) of research projects in the
# weekly-by-project chart. Anything not listed is added automatically, A-Z.
PROJECT_ORDER: list[str] = []   # e.g. ["general", "website", "thesis"]

# ======================================================================

TZ = ZoneInfo(TIMEZONE)
BASE_COLOUR = "#4C78A8"
DREAD_COLOUR = "#F58518"
NEUTRAL_COLOUR = "#8A9BB0"
PALETTE = (px.colors.qualitative.Plotly + px.colors.qualitative.Dark2
           + px.colors.qualitative.Set3)
PLOT_CONFIG = {"displaylogo": False, "responsive": True}

QUERY_TEMPLATE = """
query($login: String!, $number: Int!, $cursor: String) {
  __OWNER__(login: $login) {
    projectV2(number: $number) {
      title
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isArchived
          content {
            __typename
            ... on Issue { state closedAt }
            ... on PullRequest { state closedAt }
          }
          fieldValues(first: 30) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                updatedAt
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldDateValue {
                date
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""


# ----------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------

def fetch_items(token: str) -> tuple[list[dict], str]:
    if OWNER_TYPE not in ("user", "organization"):
        sys.exit('OWNER_TYPE must be "user" or "organization".')
    query = QUERY_TEMPLATE.replace("__OWNER__", OWNER_TYPE)
    headers = {"Authorization": f"Bearer {token}"}
    items: list[dict] = []
    cursor = None
    title = "Research Tasks"
    while True:
        resp = requests.post(
            "https://api.github.com/graphql",
            json={"query": query,
                  "variables": {"login": OWNER_LOGIN, "number": PROJECT_NUMBER,
                                "cursor": cursor}},
            headers=headers, timeout=60,
        )
        if resp.status_code == 401:
            sys.exit("GitHub rejected the token (401). Check the PROJECT_TOKEN secret "
                     "hasn't expired and has the read:project and repo scopes.")
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            sys.exit("GitHub API error:\n" + json.dumps(payload["errors"], indent=2))
        owner = (payload.get("data") or {}).get(OWNER_TYPE)
        project = (owner or {}).get("projectV2")
        if project is None:
            sys.exit(f"Couldn't find project #{PROJECT_NUMBER} for {OWNER_TYPE} "
                     f"'{OWNER_LOGIN}'. Check the settings block and token scopes.")
        title = project.get("title") or title
        conn = project["items"]
        items.extend(n for n in conn["nodes"] if n)
        if not conn["pageInfo"]["hasNextPage"]:
            return items, title
        cursor = conn["pageInfo"]["endCursor"]


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------

def to_local_date(ts: str | None) -> date | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ).date()


def label_to_number(label, mapping, field_name, warnings) -> int | None:
    if label is None:
        return None
    if label in mapping:
        return mapping[label]
    try:
        value = int(str(label).strip().lstrip("+"))
    except ValueError:
        warnings.add(f'Unrecognised {field_name} option "{label}", treated as blank.')
        return None
    warnings.add(f'{field_name} option "{label}" isn\'t in the settings; read it as {value}.')
    return value


COLUMNS = ["status", "project", "base", "dread", "scored", "hours_lo", "hours_hi",
           "deadline", "completed", "upcoming"]


def parse_items(raw: list[dict], warnings: set[str]) -> pd.DataFrame:
    rows = []
    for item in raw:
        content = item.get("content") or {}
        values: dict[str, dict] = {}
        for fv in (item.get("fieldValues") or {}).get("nodes") or []:
            field = (fv or {}).get("field") or {}
            if field.get("name"):
                values[field["name"]] = fv

        status_fv = values.get(FIELD_STATUS, {})
        status = status_fv.get("name")
        project = values.get(FIELD_PROJECT, {}).get("name") or NO_PROJECT_LABEL
        base = label_to_number(values.get(FIELD_POINTS, {}).get("name"),
                               POINTS_MAP, FIELD_POINTS, warnings)
        dread = label_to_number(values.get(FIELD_DREAD, {}).get("name"),
                                DREAD_MAP, FIELD_DREAD, warnings) or 0
        deadline_str = values.get(FIELD_DEADLINE, {}).get("date")
        deadline = date.fromisoformat(deadline_str) if deadline_str else None

        hours = None
        if base is not None:
            hours = HOURS_RANGE.get(base)
            if hours is None:
                warnings.add(f"No duration set for {base} points in HOURS_RANGE; "
                             "those tasks are left out of the hours totals.")

        completed = None
        if status == STATUS_DONE:
            status_ts, closed_ts = status_fv.get("updatedAt"), content.get("closedAt")
            first, second = ((status_ts, closed_ts) if COMPLETION_DATE_SOURCE == "status"
                             else (closed_ts, status_ts))
            completed = to_local_date(first or second)

        is_closed = content.get("state") in ("CLOSED", "MERGED")
        rows.append({
            "status": status,
            "project": project,
            "base": base or 0,
            "dread": dread,
            "scored": base is not None,
            "hours_lo": hours[0] if hours else None,
            "hours_hi": hours[1] if hours else None,
            "deadline": deadline,
            "completed": completed,
            "upcoming": (status in UPCOMING_STATUSES and not is_closed
                         and not item.get("isArchived")),
        })

    df = pd.DataFrame(rows, columns=COLUMNS)
    for col in ("base", "dread", "hours_lo", "hours_hi"):
        df[col] = pd.to_numeric(df[col])
    df["upcoming"] = df["upcoming"].astype(bool)
    df["scored"] = df["scored"].astype(bool)
    df["total"] = df["base"] + df["dread"]
    df["hours_mid"] = (df["hours_lo"] + df["hours_hi"]) / 2
    return df


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def today_local() -> date:
    return datetime.now(TZ).date()


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())   # Monday


def ordered_projects(names) -> list[str]:
    names = list(dict.fromkeys(names))
    fixed = [p for p in PROJECT_ORDER if p in names]
    rest = sorted((n for n in names if n not in fixed), key=str.lower)
    return fixed + rest


def completed_frame(df: pd.DataFrame) -> pd.DataFrame:
    done = df[df["completed"].notna()].copy()
    if not done.empty:
        done["week"] = done["completed"].map(week_start)
    return done


def weekly_index(done: pd.DataFrame) -> tuple[pd.DatetimeIndex, list[str]]:
    this_week = week_start(today_local())
    first = min(done["week"].min(), this_week)
    weeks = pd.date_range(first, this_week, freq="7D")
    labels = [f"{w:%d %b %Y}" + (" (so far)" if w.date() == this_week else "")
              for w in weeks]
    return weeks, labels


def base_layout(fig: go.Figure, height: int, legend: bool = True) -> go.Figure:
    fig.update_layout(
        template="plotly_white", height=height, bargap=0.2,
        margin=dict(l=10, r=10, t=40 if legend else 30, b=10),
        showlegend=legend,
        legend=dict(orientation="h", x=0, y=1.02, xanchor="left", yanchor="bottom"),
        font=dict(family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif", size=13),
        hoverlabel=dict(namelength=-1),
    )
    return fig


# ----------------------------------------------------------------------
# Numbers and charts
# ----------------------------------------------------------------------

def headline(df: pd.DataFrame) -> dict:
    up = df[df["upcoming"]]
    today = today_local()
    deadlines = up["deadline"].dropna()
    done = completed_frame(df)

    this_week = week_start(today)
    done_this_week = int(done.loc[done["week"] == this_week, "total"].sum()) if not done.empty else 0

    pace = None
    if not done.empty:
        full_weeks = [this_week - timedelta(weeks=i) for i in range(1, PACE_WEEKS + 1)]
        full_weeks = [w for w in full_weeks if w >= done["week"].min()]
        if full_weeks:
            pts = done[done["week"].isin(full_weeks)]["total"].sum()
            pace = (pts / len(full_weeks), len(full_weeks))

    return {
        "n": len(up),
        "by_status": {s: int((up["status"] == s).sum()) for s in UPCOMING_STATUSES},
        "base": int(up["base"].sum()),
        "dread": int(up["dread"].sum()),
        "total": int(up["total"].sum()),
        "hours_mid": float(up["hours_mid"].sum()),
        "hours_lo": float(up["hours_lo"].sum()),
        "hours_hi": float(up["hours_hi"].sum()),
        "unscored": int((~up["scored"]).sum()),
        "overdue": int(sum(d < today for d in deadlines)),
        "due_soon": int(sum(today <= d <= today + timedelta(days=DEADLINE_WINDOW_DAYS)
                            for d in deadlines)),
        "done_this_week": done_this_week,
        "pace": pace,
    }


def fig_upcoming_by_project(df: pd.DataFrame) -> go.Figure | None:
    up = df[df["upcoming"]]
    if up.empty:
        return None
    g = up.groupby("project").agg(
        tasks=("status", "size"), base=("base", "sum"), dread=("dread", "sum"),
        hours_mid=("hours_mid", "sum"), hours_lo=("hours_lo", "sum"),
        hours_hi=("hours_hi", "sum"),
    )
    g["total"] = g["base"] + g["dread"]
    g = g.sort_values(["total", "tasks"], ascending=True)   # biggest at the top
    y = g.index.tolist()

    points_title = (f"Points: <span style='color:{BASE_COLOUR}'>base</span> + "
                    f"<span style='color:{DREAD_COLOUR}'>dread</span>")
    fig = make_subplots(rows=1, cols=3, shared_yaxes=True, horizontal_spacing=0.04,
                        subplot_titles=("Tasks", points_title, "Expected hours (range)"))
    fig.add_bar(y=y, x=g["tasks"], orientation="h", marker_color=NEUTRAL_COLOUR,
                hovertemplate="%{y}: %{x} tasks<extra></extra>", row=1, col=1)
    fig.add_bar(y=y, x=g["base"], orientation="h", marker_color=BASE_COLOUR,
                hovertemplate="%{y}: %{x} base points<extra></extra>", row=1, col=2)
    fig.add_bar(y=y, x=g["dread"], orientation="h", marker_color=DREAD_COLOUR,
                customdata=g["total"],
                hovertemplate="%{y}: +%{x} dread (%{customdata} total)<extra></extra>",
                row=1, col=2)
    fig.add_bar(y=y, x=g["hours_mid"], orientation="h", marker_color=NEUTRAL_COLOUR,
                error_x=dict(type="data", symmetric=False,
                             array=(g["hours_hi"] - g["hours_mid"]).tolist(),
                             arrayminus=(g["hours_mid"] - g["hours_lo"]).tolist(),
                             color="#444", thickness=1.2, width=4),
                customdata=list(zip(g["hours_lo"], g["hours_hi"])),
                hovertemplate="%{y}: ~%{x:.1f} h (%{customdata[0]:.0f}–%{customdata[1]:.0f} h)"
                              "<extra></extra>",
                row=1, col=3)
    fig.update_yaxes(categoryorder="array", categoryarray=y)
    fig.update_layout(barmode="stack")
    return base_layout(fig, height=110 + 36 * len(y), legend=False)


def fig_daily(df: pd.DataFrame) -> go.Figure | None:
    done = completed_frame(df)
    if done.empty:
        return None
    end = max(today_local(), done["completed"].max())
    idx = pd.date_range(done["completed"].min(), end, freq="D")
    d = (done.groupby(pd.to_datetime(done["completed"]))[["base", "dread"]].sum()
         .reindex(idx, fill_value=0))
    d["total"] = d["base"] + d["dread"]
    d["avg7"] = d["total"].rolling(7, min_periods=1).mean()

    fig = go.Figure()
    fig.add_bar(x=d.index, y=d["base"], name="Base points", marker_color=BASE_COLOUR)
    fig.add_bar(x=d.index, y=d["dread"], name="Dread bump", marker_color=DREAD_COLOUR,
                customdata=d["total"],
                hovertemplate="%{y} (total %{customdata})")
    fig.add_scatter(x=d.index, y=d["avg7"], name="7-day average (total)", mode="lines",
                    line=dict(color="#333", width=2), hovertemplate="%{y:.1f}")
    fig.update_layout(barmode="stack", hovermode="x unified")
    fig.update_xaxes(
        tickformat="%a %d %b",
        rangeselector=dict(buttons=[
            dict(count=14, label="2 wk", step="day", stepmode="backward"),
            dict(count=1, label="1 mo", step="month", stepmode="backward"),
            dict(count=3, label="3 mo", step="month", stepmode="backward"),
            dict(step="all", label="All"),
        ], x=1, xanchor="right", y=1.02, yanchor="bottom"),
    )
    return base_layout(fig, height=380)


def fig_weekly(df: pd.DataFrame) -> go.Figure | None:
    done = completed_frame(df)
    if done.empty:
        return None
    weeks, labels = weekly_index(done)
    w = (done.groupby(pd.to_datetime(done["week"]))[["base", "dread"]].sum()
         .reindex(weeks, fill_value=0))
    w["total"] = w["base"] + w["dread"]

    fig = go.Figure()
    fig.add_bar(x=labels, y=w["base"], name="Base points", marker_color=BASE_COLOUR)
    fig.add_bar(x=labels, y=w["dread"], name="Dread bump", marker_color=DREAD_COLOUR,
                text=w["total"], textposition="outside", cliponaxis=False,
                customdata=w["total"], hovertemplate="%{y} (total %{customdata})")
    fig.update_layout(barmode="stack", hovermode="x unified")
    fig.update_xaxes(title_text="Week commencing (Monday)")
    return base_layout(fig, height=380)


def fig_weekly_by_project(df: pd.DataFrame, colours: dict[str, str]) -> go.Figure | None:
    done = completed_frame(df)
    if done.empty:
        return None
    weeks, labels = weekly_index(done)
    p = (done.pivot_table(index=pd.to_datetime(done["week"]), columns="project",
                          values="total", aggfunc="sum")
         .reindex(weeks).fillna(0))

    fig = go.Figure()
    for proj in ordered_projects(p.columns):
        fig.add_bar(x=labels, y=p[proj], name=proj, marker_color=colours[proj],
                    hovertemplate="%{y:.0f}")
    fig.update_layout(barmode="stack", hovermode="x unified")
    fig.update_xaxes(title_text="Week commencing (Monday)")
    return base_layout(fig, height=420)


# ----------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------

def fmt_h(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def cards_html(h: dict) -> str:
    status_bits = " · ".join(f"{n} {html.escape(s)}" for s, n in h["by_status"].items())
    hours_note = f"range {fmt_h(h['hours_lo'])}–{fmt_h(h['hours_hi'])} h"
    if h["unscored"]:
        hours_note += (f"<br>{h['unscored']} task{'s' if h['unscored'] != 1 else ''} "
                       "without Points not counted")
    if h["pace"]:
        per_week, n_weeks = h["pace"]
        pace_value = f"{per_week:.1f} pts/wk"
        weeks_left = h["total"] / per_week if per_week else None
        pace_note = f"average of last {n_weeks} full week{'s' if n_weeks != 1 else ''}"
        if weeks_left is not None:
            pace_note += f"<br>≈ {weeks_left:.1f} weeks to clear upcoming points"
    else:
        pace_value, pace_note = "–", "needs one full week of history"

    cards = [
        ("Upcoming tasks", str(h["n"]), status_bits),
        ("Upcoming points", str(h["total"]), f"{h['base']} base + {h['dread']} dread"),
        ("Expected time", f"~{fmt_h(h['hours_mid'])} h", hours_note),
        ("Deadlines", f"{h['overdue']} overdue",
         f"{h['due_soon']} due in the next {DEADLINE_WINDOW_DAYS} days"),
        ("Recent pace", pace_value, pace_note),
        ("Done this week", f"{h['done_this_week']} pts", "base + dread, since Monday"),
    ]
    return "\n".join(
        f'<div class="card"><div class="label">{label}</div>'
        f'<div class="value">{value}</div><div class="note">{note}</div></div>'
        for label, value, note in cards
    )


def render_page(df: pd.DataFrame, title: str, warnings: set[str], demo: bool,
                inline_js: bool = False) -> str:
    colours = {p: PALETTE[i % len(PALETTE)]
               for i, p in enumerate(ordered_projects(df["project"]))}
    sections = [
        ("Upcoming work by research project",
         "Tasks, points and expected hours still to do, per research project. "
         "Error bars on hours show the low–high range.",
         fig_upcoming_by_project(df)),
        ("Points completed per day",
         "Base and dread points finished on each calendar day, with a 7-day rolling average.",
         fig_daily(df)),
        ("Points completed per week",
         "Weeks run Monday–Sunday. The label on each bar is the week's total.",
         fig_weekly(df)),
        ("Weekly points by research project",
         "Total points (base + dread) finished each week, split by research project. "
         "Click a project in the legend to hide it; double-click to show it alone.",
         fig_weekly_by_project(df, colours)),
    ]

    parts, plotly_included = [], False
    for heading, blurb, fig in sections:
        if fig is None:
            body = '<p class="empty">No data yet.</p>'
        else:
            body = fig.to_html(full_html=False,
                               include_plotlyjs=(False if plotly_included
                                                 else (True if inline_js else "cdn")),
                               config=PLOT_CONFIG)
            plotly_included = True
        parts.append(f"<section><h2>{heading}</h2><p class='blurb'>{blurb}</p>{body}</section>")

    warn_html = ""
    if warnings:
        items = "".join(f"<li>{html.escape(w)}</li>" for w in sorted(warnings))
        warn_html = f"<details class='warn'><summary>Data notes ({len(warnings)})</summary><ul>{items}</ul></details>"

    updated = datetime.now(TZ).strftime("%a %d %b %Y, %H:%M")
    demo_banner = ('<div class="demo">Demo data: these numbers are made up.</div>'
                   if demo else "")
    n_done = int(df["completed"].notna().sum())

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Research Dashboard</title>
<style>
  :root {{ --bg:#f6f7f9; --panel:#fff; --ink:#1d2430; --muted:#5f6b7a; --line:#e3e7ec; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:1100px; margin:0 auto; padding:24px 16px 48px; }}
  header h1 {{ margin:0 0 4px; font-size:1.6rem; }}
  header p {{ margin:0 0 20px; color:var(--muted); }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-bottom:24px; }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
  .card .label {{ font-size:.8rem; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  .card .value {{ font-size:1.7rem; font-weight:650; margin:2px 0; }}
  .card .note {{ font-size:.85rem; color:var(--muted); }}
  section {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
            padding:16px; margin-bottom:18px; }}
  section h2 {{ margin:0 0 2px; font-size:1.1rem; }}
  .blurb {{ margin:0 0 8px; color:var(--muted); font-size:.9rem; }}
  .empty {{ color:var(--muted); font-style:italic; }}
  .warn {{ font-size:.85rem; color:var(--muted); margin-top:12px; }}
  .demo {{ background:#fff4d6; border:1px solid #f0d58a; padding:8px 12px; border-radius:8px; margin-bottom:16px; }}
  footer {{ color:var(--muted); font-size:.85rem; margin-top:12px; }}
</style>
</head>
<body>
<main>
<header>
  <h1>Research Dashboard</h1>
  <p>{html.escape(title)} · updated {updated} ({TIMEZONE})</p>
</header>
{demo_banner}
<div class="cards">
{cards_html(headline(df))}
</div>
{''.join(parts)}
<footer>{len(df)} items in the project · {n_done} completed with a completion date.</footer>
{warn_html}
</main>
</body>
</html>
"""


# ----------------------------------------------------------------------
# Demo data
# ----------------------------------------------------------------------

def demo_items(n: int = 140, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    projects = ["general", "website", "thesis-ch3", "grant-renewal",
                "survey-paper", "fieldwork-2026", "collab-modelling"]
    now = datetime.now(timezone.utc)

    def sv(field, name, ts=None):
        return {"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": name,
                "updatedAt": ts, "field": {"name": field}}

    items = []
    for _ in range(n):
        done = rnd.random() < 0.65
        status = STATUS_DONE if done else rnd.choice(
            ["Todo", "Todo", "Todo", "In Progress", "Paused / Background"])
        ts = (now - timedelta(days=rnd.uniform(0, 45))).isoformat().replace("+00:00", "Z")
        nodes = [sv(FIELD_STATUS, status, ts),
                 sv(FIELD_PROJECT, rnd.choice(projects)),
                 sv(FIELD_DREAD, rnd.choices(["+0", "+1", "+2"], [6, 3, 1])[0])]
        if rnd.random() > 0.05:
            nodes.append(sv(FIELD_POINTS, rnd.choices(list("12345"), [3, 4, 3, 2, 1])[0]))
        if not done and rnd.random() < 0.25:
            dl = (now + timedelta(days=rnd.randint(-4, 30))).date().isoformat()
            nodes.append({"__typename": "ProjectV2ItemFieldDateValue", "date": dl,
                          "field": {"name": FIELD_DEADLINE}})
        items.append({
            "isArchived": False,
            "content": {"__typename": "Issue", "state": "CLOSED" if done else "OPEN",
                        "closedAt": ts if done else None},
            "fieldValues": {"nodes": nodes},
        })
    return items


# ----------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Build the research tasks dashboard.")
    ap.add_argument("--out", default="site/index.html", help="output HTML path")
    ap.add_argument("--demo", action="store_true", help="use made-up data")
    ap.add_argument("--inline-js", action="store_true",
                    help="embed the chart library in the page (works offline, ~4 MB larger)")
    args = ap.parse_args()

    if args.demo:
        raw, title = demo_items(), "Demo project"
    else:
        token = os.environ.get("PROJECT_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            sys.exit("No token found. Set the PROJECT_TOKEN environment variable "
                     "(or run with --demo to preview).")
        if OWNER_LOGIN.startswith("YOUR-") or not PROJECT_NUMBER:
            sys.exit("Fill in OWNER_LOGIN and PROJECT_NUMBER in the settings block first.")
        raw, title = fetch_items(token)

    warnings: set[str] = set()
    df = parse_items(raw, warnings)
    page = render_page(df, title, warnings, demo=args.demo, inline_js=args.inline_js)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"Wrote {out}: {len(df)} items, {int(df['upcoming'].sum())} upcoming, "
          f"{int(df['completed'].notna().sum())} completed.")
    for w in sorted(warnings):
        print("Note:", w)


if __name__ == "__main__":
    main()
