#!/usr/bin/env python3
"""Sprint Metrics Analyzer.

Reads a Jira-style CSV export and produces:
  - velocity chart (committed vs completed, with average + rolling average)
  - sprint burndown charts (one per sprint)
  - cycle time stats and histogram
  - a markdown sprint report

Standard library only. Run:  python sprint_metrics.py
If the input CSV does not exist, 6 sprints of sample data are generated first.
"""
import argparse
import csv
import html
import json
import math
import os
import random
import statistics
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DT_FMT = "%Y-%m-%d %H:%M"
D_FMT = "%Y-%m-%d"
SPRINT_DAYS = 14
FIELDS = ["Issue key", "Summary", "Issue Type", "Status", "Story Points", "Assignee",
          "Sprint", "Sprint Start", "Sprint End", "Created", "Started", "Resolved"]


# --------------------------------------------------------------------------- sample data

def generate_sample(path: Path, sprints: int = 6, seed: int = 42, now: datetime | None = None) -> None:
    """Write sample data ending with an in-progress sprint (6 days in) so the live dashboard has something to show."""
    rng = random.Random(seed)
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    people = ["Aisha", "Rohan", "Mei", "Lucas", "Priya"]
    verbs = ["Add", "Fix", "Refactor", "Document", "Optimise", "Migrate", "Test"]
    things = ["login flow", "search index", "billing export", "user profile", "email digest",
              "API rate limits", "dashboard widgets", "CI pipeline", "audit log", "mobile layout"]
    first_start = now.date() - timedelta(days=SPRINT_DAYS * (sprints - 1) + 6)
    rows, n = [], 100
    for s in range(1, sprints + 1):
        start = first_start + timedelta(days=SPRINT_DAYS * (s - 1))
        end = start + timedelta(days=SPRINT_DAYS - 1)
        deadline = datetime.combine(end, datetime.min.time()).replace(hour=17)
        fail_rate = 0.4 if s == 4 else 0.12  # sprint 4 is the "bad" sprint
        count = rng.randint(11, 15)
        for i in range(count):
            n += 1
            pts = rng.choice([1, 2, 3, 3, 5, 5, 8])
            itype = rng.choices(["Story", "Bug", "Task"], [5, 3, 2])[0]
            added = i >= count - 2 and rng.random() < 0.7  # mid-sprint scope creep
            if added:
                created = datetime.combine(start + timedelta(days=rng.randint(2, 7)), datetime.min.time()).replace(hour=10)
                started = created + timedelta(hours=rng.randint(1, 30))
            else:
                created = datetime.combine(start - timedelta(days=rng.randint(1, 10)), datetime.min.time()).replace(hour=11)
                started = datetime.combine(start + timedelta(days=rng.randint(0, 6)), datetime.min.time()).replace(hour=rng.randint(9, 16))
            if created > now:
                continue
            cycle = max(0.4, rng.gauss(1.2 + pts * 0.55 + (-0.5 if itype == "Bug" else 0), 1.0))
            resolved = started + timedelta(days=cycle)
            if started > now:
                status, started, resolved = "To Do", None, None
            elif resolved > min(deadline, now) or rng.random() < fail_rate:
                status = rng.choice(["In Progress", "To Do", "In Review"])
                resolved = None
                if status == "To Do":
                    started = None
            else:
                status = "Done"
            rows.append({
                "Issue key": f"PROJ-{n}",
                "Summary": f"{rng.choice(verbs)} {rng.choice(things)}",
                "Issue Type": itype, "Status": status, "Story Points": pts,
                "Assignee": rng.choice(people), "Sprint": f"Sprint {s}",
                "Sprint Start": start.strftime(D_FMT), "Sprint End": end.strftime(D_FMT),
                "Created": created.strftime(DT_FMT),
                "Started": started.strftime(DT_FMT) if started else "",
                "Resolved": resolved.strftime(DT_FMT) if resolved else "",
            })
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------- loading

@dataclass
class Issue:
    key: str
    type: str
    status: str
    points: float
    sprint: str
    created: datetime
    started: datetime | None
    resolved: datetime | None
    summary: str = ""
    assignee: str = ""


@dataclass
class Sprint:
    name: str
    start: date
    end: date
    issues: list


def _dt(s: str):
    return datetime.strptime(s, DT_FMT) if s.strip() else None


def load_sprints(path: Path) -> list[Sprint]:
    try:
        return _read_sprints(path)
    except KeyError as e:
        raise ValueError(f"CSV is missing column {e}") from None


def _read_sprints(path: Path) -> list[Sprint]:
    sprints: dict[str, Sprint] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            name = r["Sprint"].strip()
            if name not in sprints:
                sprints[name] = Sprint(name, datetime.strptime(r["Sprint Start"], D_FMT).date(),
                                       datetime.strptime(r["Sprint End"], D_FMT).date(), [])
            sprints[name].issues.append(Issue(
                r["Issue key"], r["Issue Type"], r["Status"], float(r["Story Points"] or 0), name,
                _dt(r["Created"]), _dt(r["Started"]), _dt(r["Resolved"]),
                summary=r.get("Summary", ""), assignee=r.get("Assignee", "")))
    return sorted(sprints.values(), key=lambda s: s.start)


# --------------------------------------------------------------------------- metrics

def completed(sp: Sprint):
    return [i for i in sp.issues if i.resolved and i.resolved.date() <= sp.end]


def velocity_rows(sprints):
    rows = []
    for sp in sprints:
        committed = sum(i.points for i in sp.issues if i.created.date() <= sp.start)
        added = sum(i.points for i in sp.issues if i.created.date() > sp.start)
        done = sum(i.points for i in completed(sp))
        rows.append({"sprint": sp.name, "committed": committed, "added": added, "completed": done,
                     "rate": done / (committed + added) if committed + added else 0})
    return rows


def burndown(sp: Sprint):
    """Remaining points at end of each sprint day, plus the ideal line from committed scope."""
    days = (sp.end - sp.start).days + 1
    remaining = []
    for d in range(days):
        day = sp.start + timedelta(days=d)
        scope = sum(i.points for i in sp.issues if i.created.date() <= day)
        done = sum(i.points for i in completed(sp) if i.resolved.date() <= day)
        remaining.append(scope - done)
    committed = sum(i.points for i in sp.issues if i.created.date() <= sp.start)
    ideal = [committed * (1 - d / (days - 1)) for d in range(days)]
    return remaining, ideal


def percentile(values, p):
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def cycle_times(issues):
    return [(i.resolved - i.started).total_seconds() / 86400 for i in issues if i.started and i.resolved]


def cycle_stats(values):
    if not values:
        return None
    return {"n": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "p85": percentile(values, 85), "p95": percentile(values, 95),
            "min": min(values), "max": max(values)}


# --------------------------------------------------------------------------- SVG charts

W, H, ML, MR, MT, MB = 760, 400, 60, 24, 64, 64
PW, PH = W - ML - MR, H - MT - MB
C_TEXT, C_GRID = "#333", "#e3e6ea"
C_LIGHT, C_MAIN, C_AVG, C_ROLL, C_IDEAL = "#b8c4d6", "#2f6fdb", "#e07b1a", "#1a9c6b", "#8a8f98"


def nice_max(v):
    if v <= 0:
        return 1
    exp = 10 ** math.floor(math.log10(v))
    return next(m * exp for m in (1, 2, 5, 10) if v <= m * exp)


def _frame(title, ymax, ylabel, xlabel, legend):
    y = lambda v: MT + PH * (1 - v / ymax)
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
         f'font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12" fill="{C_TEXT}">',
         f'<rect width="{W}" height="{H}" fill="#fff"/>',
         f'<text x="{ML}" y="26" font-size="16" font-weight="600">{html.escape(title)}</text>']
    for k in range(6):
        v = ymax * k / 5
        p.append(f'<line x1="{ML}" x2="{W - MR}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{C_GRID}"/>')
        p.append(f'<text x="{ML - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
    p.append(f'<text x="{ML + PW / 2}" y="{H - 10}" text-anchor="middle">{html.escape(xlabel)}</text>')
    p.append(f'<text transform="translate(16 {MT + PH / 2}) rotate(-90)" text-anchor="middle">{html.escape(ylabel)}</text>')
    x = ML
    for label, color, dashed in legend:
        dash = ' stroke-dasharray="5 3"' if dashed else ""
        p.append(f'<line x1="{x}" x2="{x + 18}" y1="46" y2="46" stroke="{color}" stroke-width="3"{dash}/>')
        p.append(f'<text x="{x + 24}" y="50">{html.escape(label)}</text>')
        x += 40 + 7 * len(label)
    return p, y


def _finish(path: Path, parts):
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def _polyline(xs, ys, color, dashed=False, markers=False):
    pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in zip(xs, ys))
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    out = [f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.5"{dash}/>']
    if markers:
        out += [f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3.5" fill="{color}"/>' for a, b in zip(xs, ys)]
    return out


def velocity_chart(path, rows):
    done = [r["completed"] for r in rows]
    avg = statistics.mean(done)
    roll = [statistics.mean(done[max(0, i - 2):i + 1]) for i in range(len(done))]
    ymax = nice_max(max(max(r["committed"] + r["added"] for r in rows), max(roll)) * 1.1)
    p, y = _frame("Velocity: committed vs completed story points", ymax, "Story points", "Sprint",
                  [("Committed + added", C_LIGHT, False), ("Completed", C_MAIN, False),
                   (f"Average ({avg:.1f})", C_AVG, True), ("3-sprint rolling avg", C_ROLL, False)])
    bw = PW / len(rows)
    cx = []
    for i, r in enumerate(rows):
        x0 = ML + i * bw
        cx.append(x0 + bw / 2)
        for j, (val, col) in enumerate(((r["committed"] + r["added"], C_LIGHT), (r["completed"], C_MAIN))):
            bx = x0 + bw * (0.12 + 0.38 * j)
            p.append(f'<rect x="{bx:.1f}" y="{y(val):.1f}" width="{bw * 0.36:.1f}" height="{y(0) - y(val):.1f}" fill="{col}"/>')
            p.append(f'<text x="{bx + bw * 0.18:.1f}" y="{y(val) - 4:.1f}" text-anchor="middle" font-size="11">{val:g}</text>')
        p.append(f'<text x="{x0 + bw / 2:.1f}" y="{MT + PH + 20}" text-anchor="middle">{html.escape(r["sprint"])}</text>')
    p.append(f'<line x1="{ML}" x2="{W - MR}" y1="{y(avg):.1f}" y2="{y(avg):.1f}" stroke="{C_AVG}" stroke-width="2" stroke-dasharray="6 4"/>')
    p += _polyline(cx, [y(v) for v in roll], C_ROLL, markers=True)
    _finish(path, p)


def burndown_chart(path, sp: Sprint):
    remaining, ideal = burndown(sp)
    n = len(remaining)
    ymax = nice_max(max(remaining + ideal) * 1.1)
    p, y = _frame(f"{sp.name} burndown", ymax, "Remaining story points", "Sprint day",
                  [("Ideal", C_IDEAL, True), ("Remaining", C_MAIN, False)])
    xs = [ML + PW * d / (n - 1) for d in range(n)]
    for d, x in enumerate(xs):
        if d % 2 == 0 or d == n - 1:
            p.append(f'<text x="{x:.1f}" y="{MT + PH + 20}" text-anchor="middle">{d}</text>')
    p += _polyline(xs, [y(v) for v in ideal], C_IDEAL, dashed=True)
    p += _polyline(xs, [y(v) for v in remaining], C_MAIN, markers=True)
    _finish(path, p)


def cycle_histogram(path, values):
    top = max(1, math.ceil(max(values)))
    counts = [0] * top
    for v in values:
        counts[min(int(v), top - 1)] += 1
    ymax = nice_max(max(counts) * 1.1)
    med, p85 = statistics.median(values), percentile(values, 85)
    p, y = _frame("Cycle time distribution (In Progress to Done)", ymax, "Issues", "Days",
                  [(f"Median {med:.1f}d", C_AVG, True), (f"85th pct {p85:.1f}d", C_ROLL, True)])
    bw = PW / top
    for i, c in enumerate(counts):
        x0 = ML + i * bw
        p.append(f'<rect x="{x0 + 1:.1f}" y="{y(c):.1f}" width="{bw - 2:.1f}" height="{y(0) - y(c):.1f}" fill="{C_MAIN}"/>')
        if c:
            p.append(f'<text x="{x0 + bw / 2:.1f}" y="{y(c) - 4:.1f}" text-anchor="middle" font-size="11">{c}</text>')
        p.append(f'<text x="{x0 + bw / 2:.1f}" y="{MT + PH + 20}" text-anchor="middle">{i}-{i + 1}</text>')
    for val, col in ((med, C_AVG), (p85, C_ROLL)):
        x = ML + PW * min(val, top) / top
        p.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{MT}" y2="{MT + PH}" stroke="{col}" stroke-width="2" stroke-dasharray="6 4"/>')
    _finish(path, p)


# --------------------------------------------------------------------------- report

def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def build_report(sprints, vel, bd_sprint, out_dir: Path, source: Path) -> str:
    done = [r["completed"] for r in vel]
    last3 = done[-3:]
    all_issues = [i for sp in sprints for i in sp.issues]
    ct_all = cycle_times(all_issues)
    stats = cycle_stats(ct_all)
    best = max(vel, key=lambda r: r["completed"])
    worst = min(vel, key=lambda r: r["completed"])
    prev_avg = statistics.mean(done[:-1]) if len(done) > 1 else done[0]
    delta = (done[-1] - prev_avg) / prev_avg * 100 if prev_avg else 0
    creep = statistics.mean(r["added"] / r["committed"] for r in vel if r["committed"]) * 100
    avg_rate = statistics.mean(r["rate"] for r in vel) * 100

    L = [f"# Sprint Report: {sprints[0].name} to {sprints[-1].name}", "",
         f"_Source: `{source.name}` | {len(all_issues)} issues | "
         f"{sprints[0].start:%d %b %Y} to {sprints[-1].end:%d %b %Y}_", "",
         "## Highlights", "",
         f"- **Average velocity:** {statistics.mean(done):.1f} pts/sprint "
         f"(best {best['sprint']}: {best['completed']:g}, lowest {worst['sprint']}: {worst['completed']:g}).",
         f"- **Latest sprint ({vel[-1]['sprint']}):** {done[-1]:g} pts, {delta:+.0f}% vs the average of earlier sprints.",
         f"- **Forecast for next sprint:** about {statistics.mean(last3):.0f} pts "
         f"(range {min(last3):g}-{max(last3):g}, based on the last {len(last3)} sprints).",
         f"- **Completion rate:** {avg_rate:.0f}% of scope finished on average.",
         f"- **Scope creep:** {creep:.0f}% of committed points are added mid-sprint on average.",
         f"- **Cycle time:** median {stats['median']:.1f} days; 85% of items finish within {stats['p85']:.1f} days.",
         "", "## Velocity", "", "![Velocity](charts/velocity.svg)", "",
         md_table(["Sprint", "Committed", "Added", "Completed", "Completion"],
                  [(r["sprint"], f"{r['committed']:g}", f"{r['added']:g}", f"{r['completed']:g}", f"{r['rate']:.0%}")
                   for r in vel]),
         "", f"## Burndown: {bd_sprint.name}", "", f"![Burndown](charts/burndown_{_slug(bd_sprint.name)}.svg)", ""]
    remaining, _ = burndown(bd_sprint)
    L += [f"Remaining at sprint end: **{remaining[-1]:g} pts**. "
          "Burndown charts for every sprint are in `charts/`.", "",
          "## Cycle time", "", "![Cycle time](charts/cycle_time.svg)", "", "By sprint (days):", ""]
    rows = []
    for sp in sprints:
        s = cycle_stats(cycle_times(sp.issues))
        if s:
            rows.append((sp.name, s["n"], f"{s['mean']:.1f}", f"{s['median']:.1f}", f"{s['p85']:.1f}", f"{s['p95']:.1f}", f"{s['max']:.1f}"))
    rows.append(("**All**", stats["n"], f"{stats['mean']:.1f}", f"{stats['median']:.1f}", f"{stats['p85']:.1f}", f"{stats['p95']:.1f}", f"{stats['max']:.1f}"))
    L += [md_table(["Sprint", "Items", "Mean", "Median", "P85", "P95", "Max"], rows), "", "By issue type (days):", ""]
    rows = []
    for t in sorted({i.type for i in all_issues}):
        s = cycle_stats(cycle_times([i for i in all_issues if i.type == t]))
        if s:
            rows.append((t, s["n"], f"{s['mean']:.1f}", f"{s['median']:.1f}", f"{s['p85']:.1f}"))
    L += [md_table(["Type", "Items", "Mean", "Median", "P85"], rows), "",
          "## Definitions", "",
          "- **Committed:** points on issues created on or before sprint start. **Added:** created after sprint start.",
          "- **Completed:** points on issues resolved within the sprint dates.",
          "- **Cycle time:** Started (moved to In Progress) to Resolved, in calendar days.",
          "- **P85/P95:** 85% / 95% of items finished within this many days.", ""]
    return "\n".join(L)


def _slug(name):
    return name.lower().replace(" ", "_")


# --------------------------------------------------------------------------- live dashboard

def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M") if d else None


def filter_sprints(sprints, itype="", assignee=""):
    return [Sprint(sp.name, sp.start, sp.end,
                   [i for i in sp.issues if (not itype or i.type == itype) and (not assignee or i.assignee == assignee)])
            for sp in sprints]


def build_metrics(sprints, today: date | None = None) -> dict:
    """Everything the dashboard needs, as JSON-serialisable data."""
    today = today or date.today()
    vel = velocity_rows(sprints)
    done = [r["completed"] for r in vel]
    origin = sprints[0].start
    points = [{"key": i.key, "type": i.type, "assignee": i.assignee, "sprint": sp.name,
               "days": (i.resolved - i.started).total_seconds() / 86400,
               "x": (i.resolved - datetime.combine(origin, datetime.min.time())).total_seconds() / 86400}
              for sp in sprints for i in sp.issues if i.started and i.resolved]
    stats = cycle_stats([p["days"] for p in points])
    last3 = done[-3:]
    bd, table, issues = {}, [], []
    for sp, v in zip(sprints, vel):
        remaining, ideal = burndown(sp)
        active = sp.start <= today <= sp.end
        if active:
            remaining = remaining[:(today - sp.start).days + 1]
        elif sp.start > today:
            remaining = []
        ct = cycle_stats(cycle_times(sp.issues))
        state = "active" if active else ("future" if sp.start > today else "closed")
        bd[sp.name] = {"ideal": ideal, "remaining": remaining, "days": len(ideal)}
        table.append({**v, "start": sp.start.isoformat(), "end": sp.end.isoformat(), "state": state,
                      "issues": len(sp.issues), "done": len(completed(sp)),
                      "median_cycle": ct["median"] if ct else None})
        issues += [{"key": i.key, "summary": i.summary, "type": i.type, "status": i.status, "points": i.points,
                    "assignee": i.assignee, "sprint": sp.name, "started": _iso(i.started), "resolved": _iso(i.resolved)}
                   for i in sp.issues]
    return {
        "origin": origin.isoformat(), "today": today.isoformat(),
        "kpis": {"avg_velocity": statistics.mean(done), "forecast": statistics.mean(last3),
                 "forecast_min": min(last3), "forecast_max": max(last3),
                 "completion": statistics.mean(r["rate"] for r in vel),
                 "creep": statistics.mean(r["added"] / r["committed"] for r in vel if r["committed"]) if any(r["committed"] for r in vel) else 0},
        "velocity": vel, "sprints": table, "burndown": bd, "cycle": {"points": points, "stats": stats},
        "issues": issues,
    }


class LiveState:
    """Tracks the CSV on disk; re-reads it whenever its mtime/size changes."""

    def __init__(self, path: Path):
        self.path, self.lock, self._cache = path, threading.Lock(), (None, None)

    def version(self) -> str:
        st = self.path.stat()
        return f"{st.st_mtime_ns}-{st.st_size}"

    def sprints(self):
        with self.lock:  # a read must not overlap the atomic file swap (Windows refuses to replace an open file)
            v = self.version()
            if self._cache[0] != v:
                self._cache = (v, load_sprints(self.path))
            return self._cache

    def replace_with(self, write):
        """Run write(tmp_path), validate the result, then atomically swap it in."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        with self.lock:
            try:
                write(tmp)
                if not load_sprints(tmp):
                    raise ValueError("CSV has no rows")
                for attempt in range(5):  # antivirus/Excel can briefly lock the file
                    try:
                        os.replace(tmp, self.path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.1)
            except PermissionError:
                raise ValueError("Cannot write the CSV; is it open in another program?") from None
            finally:
                tmp.unlink(missing_ok=True)

    def simulate(self):
        """Advance one issue of the active sprint (To Do -> In Progress -> Done) to mimic live Jira activity."""
        info = {}

        def write(tmp):
            with self.path.open(newline="", encoding="utf-8-sig") as f:
                rd = csv.DictReader(f)
                names, rows = rd.fieldnames, list(rd)
            today = date.today()
            latest = max(r["Sprint Start"] for r in rows)
            if datetime.strptime(next(r["Sprint End"] for r in rows if r["Sprint Start"] == latest), D_FMT).date() < today:
                raise ValueError("The latest sprint has ended; nothing to simulate. Use Generate sample for a fresh active sprint.")
            open_rows = [r for r in rows if r["Sprint Start"] == latest and r["Status"] != "Done"]
            if not open_rows:
                raise ValueError("Every issue in the active sprint is already Done.")
            r = random.choice(open_rows)
            now = datetime.now().strftime(DT_FMT)
            if r["Status"] == "To Do":
                r["Status"], r["Started"] = "In Progress", now
            else:
                r["Status"], r["Resolved"] = "Done", now
                r["Started"] = r["Started"] or now
            info.update(key=r["Issue key"], status=r["Status"])
            with tmp.open("w", newline="", encoding="utf-8") as out:
                w = csv.DictWriter(out, fieldnames=names)
                w.writeheader()
                w.writerows(rows)

        self.replace_with(write)
        return info


def make_handler(state: LiveState, dashboard: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode())

        def _guard(self, fn):
            try:
                fn()
            except (ValueError, OSError, KeyError, csv.Error) as e:
                self._json({"error": str(e) or e.__class__.__name__}, 422)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._guard(lambda: self._send(200, dashboard.read_bytes(), "text/html; charset=utf-8"))
            elif url.path == "/api/metrics":
                self._guard(lambda: self._metrics(parse_qs(url.query)))
            else:
                self._send(404, b'{"error":"not found"}')

        def _metrics(self, q):
            version, sprints = state.sprints()
            if not sprints:
                raise ValueError("CSV has no rows")
            if q.get("since", [""])[0] == version + q.get("f", [""])[0]:
                return self._json({"unchanged": True})
            itype, assignee = q.get("type", [""])[0], q.get("assignee", [""])[0]
            data = build_metrics(filter_sprints(sprints, itype, assignee))
            data.update(version=version, file=state.path.name,
                        types=sorted({i.type for sp in sprints for i in sp.issues}),
                        assignees=sorted({i.assignee for sp in sprints for i in sp.issues if i.assignee}))
            self._json(data)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/upload":
                self._guard(self._upload)
            elif path == "/api/sample":
                self._guard(lambda: (state.replace_with(generate_sample), self._json({"ok": True}))[1])
            elif path == "/api/simulate":
                self._guard(lambda: self._json(state.simulate()))
            else:
                self._send(404, b'{"error":"not found"}')

        def _upload(self):
            size = int(self.headers.get("Content-Length") or 0)
            if not 0 < size <= 10_000_000:
                raise ValueError("Upload must be a CSV under 10 MB")
            body = self.rfile.read(size)
            state.replace_with(lambda tmp: tmp.write_bytes(body))
            self._json({"ok": True})

    return Handler


def serve(path: Path, port: int, open_browser: bool):
    if not path.exists():
        generate_sample(path)
        print(f"Generated sample data: {path}")
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(LiveState(path), Path(__file__).with_name("dashboard.html")))
    url = f"http://localhost:{port}"
    print(f"Live dashboard on {url} (watching {path}; Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Analyze a Jira-style CSV export: velocity, burndown, cycle time.")
    ap.add_argument("--input", default="data/sprint_export.csv", type=Path, help="Jira CSV export")
    ap.add_argument("--output", default="output", type=Path, help="output directory")
    ap.add_argument("--generate", action="store_true", help="(re)generate sample data into --input first")
    ap.add_argument("--sprint", help='sprint to feature in the report burndown (default: last), e.g. "Sprint 3"')
    ap.add_argument("--serve", action="store_true", help="run the live dashboard on localhost instead of writing a static report")
    ap.add_argument("--port", type=int, default=8000, help="dashboard port (default 8000)")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    args = ap.parse_args()

    if args.serve:
        if args.generate:
            generate_sample(args.input)
        return serve(args.input, args.port, not args.no_open)

    if args.generate or not args.input.exists():
        generate_sample(args.input)
        print(f"Generated sample data: {args.input}")

    sprints = load_sprints(args.input)
    if not sprints:
        raise SystemExit("No sprints found in input.")
    featured = next((s for s in sprints if s.name.lower() == (args.sprint or "").lower()), sprints[-1])
    if args.sprint and featured.name.lower() != args.sprint.lower():
        raise SystemExit(f"Unknown sprint {args.sprint!r}. Available: {', '.join(s.name for s in sprints)}")

    charts = args.output / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    vel = velocity_rows(sprints)
    velocity_chart(charts / "velocity.svg", vel)
    for sp in sprints:
        burndown_chart(charts / f"burndown_{_slug(sp.name)}.svg", sp)
    all_ct = cycle_times([i for sp in sprints for i in sp.issues])
    if not all_ct:
        raise SystemExit("No issues with both Started and Resolved dates; cannot compute cycle time.")
    cycle_histogram(charts / "cycle_time.svg", all_ct)

    report = args.output / "sprint_report.md"
    report.write_text(build_report(sprints, vel, featured, args.output, args.input), encoding="utf-8")

    print(f"{'Sprint':<10}{'Committed':>10}{'Added':>7}{'Completed':>10}{'Rate':>7}")
    for r in vel:
        print(f"{r['sprint']:<10}{r['committed']:>10g}{r['added']:>7g}{r['completed']:>10g}{r['rate']:>7.0%}")
    s = cycle_stats(all_ct)
    print(f"Cycle time: median {s['median']:.1f}d, p85 {s['p85']:.1f}d, p95 {s['p95']:.1f}d ({s['n']} items)")
    print(f"Report: {report}")


if __name__ == "__main__":
    main()
