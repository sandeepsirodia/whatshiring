"""whatshiring: what's actually hiring this week, what's rising, and what fits you.

Pulls public job boards (Greenhouse, Lever, Ashby, RemoteOK: no logins, no scraping of logged-in
sites), keeps snapshots in SQLite, and reports which skills are rising or falling with intervals,
which companies are ramping up, and which open roles fit your resume and the roles you gave it.
Standard library only.
"""
import argparse
import html
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from statistics import NormalDist, median

__version__ = "0.1.0"
DAY = 86400.0
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".applyloop", "jobs.sqlite")
USER_AGENT = "whatshiring/%s (+https://github.com/sandeepsirodia/whatshiring)" % __version__
WINDOW = 28            # days per comparison window
MIN_TREND = 50         # postings needed on each side before a trend verdict
PER_COMPANY = 20       # at most this many postings per company per window, so one big hirer can't be "the market"
ENGINEERING = ("backend", "frontend", "fullstack", "infra", "ml/ai", "data", "mobile", "security", "embedded", "fde", "engineering")

# Overridable so tests can point at a local server.
URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{org}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{org}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true",
    "remoteok": "https://remoteok.com/api",
    "hn_threads": "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=20",
    "hn_item": "https://hn.algolia.com/api/v1/items/{id}",
}

ROLE_FAMILIES = [
    # first: "Forward Deployed Engineer, AI" is an FDE role, not an ML one
    ("fde", r"\bforward[- ]deployed\b|\bdeployment strategist\b|\bsolutions? engineer\b|\bfield engineer\b|\bfde\b"),
    ("manager", r"\b(engineering manager|manager, engineering|head of engineering|director of engineering|vp,? engineering)\b"),
    ("ml/ai", r"\b(machine learning|ml|ai|llm|deep learning|research (engineer|scientist)|applied scientist|inference|model)\b.*\b(engineer|scientist|researcher)\b|\b(ai|ml) engineer\b"),
    ("data", r"\b(data (engineer|scientist|analyst|platform)|analytics engineer|bi engineer)\b"),
    ("security", r"\b(security|appsec|infosec|detection) (engineer|analyst|architect)\b|\bsecurity\b.*\bengineer\b"),
    ("infra", r"\b(infrastructure|infra|platform|sre|site reliability|devops|cloud|reliability|production engineer|systems engineer)\b"),
    ("mobile", r"\b(ios|android|mobile|react native|flutter)\b.*\b(engineer|developer)\b"),
    ("embedded", r"\b(embedded|firmware|hardware|fpga|asic|robotics)\b.*\b(engineer)\b"),
    ("frontend", r"\b(front[- ]?end|ui engineer|web engineer|design engineer)\b"),
    ("fullstack", r"\bfull[- ]?stack\b"),
    ("backend", r"\b(back[- ]?end|api|server|distributed systems|software engineer|software developer|swe)\b"),
    ("engineering", r"\b(engineer|developer)\b"),
]
ROLE_FAMILIES = [(f, re.compile(rx, re.I)) for f, rx in ROLE_FAMILIES]
SENIORITY = [
    ("intern", r"\b(intern|internship|co-?op)\b"), ("principal", r"\b(principal|distinguished|fellow)\b"),
    ("staff", r"\bstaff\b"), ("lead", r"\b(lead|tech lead)\b"), ("senior", r"\b(senior|sr\.?|iii|l5)\b"),
    ("junior", r"\b(junior|jr\.?|entry[- ]level|new grad|graduate|associate)\b"),
]
SENIORITY = [(s, re.compile(rx, re.I)) for s, rx in SENIORITY]
LEVEL_ORDER = ["intern", "junior", "mid", "senior", "lead", "staff", "principal"]


def role_family(title):
    for fam, rx in ROLE_FAMILIES:
        if rx.search(title or ""):
            return fam
    return "other"


def seniority(title):
    for s, rx in SENIORITY:
        if rx.search(title or ""):
            return s
    return "unspecified"


# ------------------------------------------------------------------ skills

def load_skills(path=None):
    with open(path or os.path.join(HERE, "skills.json"), encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for s in data["skills"]:
        parts = []
        if s.get("aliases"):
            alts = "|".join(re.escape(a) for a in sorted(s["aliases"], key=len, reverse=True))
            parts.append(re.compile(r"(?<![\w+#.])(?:%s)(?![\w+#])" % alts, re.I))
        if s.get("context"):
            parts.append(re.compile(s["context"]))
        out.append((s["name"], parts))
    return out


SKILLS = load_skills()


def skills_in(text, taxonomy=None):
    return sorted({name for name, rxs in (taxonomy or SKILLS) if any(rx.search(text or "") for rx in rxs)})


# ------------------------------------------------------------------ statistics (vendored from lucky)

def wilson(k, n, conf=0.95):
    if n == 0:
        return 0.0, 1.0
    z = NormalDist().inv_cdf(1 - (1 - conf) / 2)
    p = k / n
    denom, centre = 1 + z * z / n, p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (0.0 if k == 0 else max(0.0, (centre - half) / denom)), (1.0 if k == n else min(1.0, (centre + half) / denom))


def _log_comb(n, k):
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact(a, b, c, d):
    """Two-sided Fisher exact p for [[a, b], [c, d]]."""
    n, row1, col1 = a + b + c + d, a + b, a + c
    lo, hi = max(0, col1 - (n - row1)), min(row1, col1)
    logp = lambda x: _log_comb(row1, x) + _log_comb(n - row1, col1 - x) - _log_comb(n, col1)  # noqa: E731
    observed = logp(a)
    return min(1.0, sum(math.exp(logp(x)) for x in range(lo, hi + 1) if logp(x) <= observed + 1e-7))


def holm(pvalues):
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted, running = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted


def newcombe(k1, n1, k2, n2):
    """95% interval for p1 - p2 (Newcombe's hybrid score method, from two Wilson intervals)."""
    p1, p2 = k1 / n1, k2 / n2
    (l1, u1), (l2, u2) = wilson(k1, n1), wilson(k2, n2)
    d = p1 - p2
    return d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2), d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)


# ------------------------------------------------------------------ polite HTTP

class Http:
    """At most one request per `gap` seconds per host; honours 429/503 Retry-After; a descriptive User-Agent."""

    def __init__(self, gap=1.0, retries=3, timeout=60, sleep=time.sleep):
        self.gap, self.retries, self.timeout, self.sleep = gap, retries, timeout, sleep
        self.last, self.lock = {}, threading.Lock()
        self.requests = []  # (host, monotonic time) for tests and --verbose

    def _wait(self, host):
        with self.lock:
            now = time.monotonic()
            due = self.last.get(host, -1e9) + self.gap
            self.last[host] = max(now, due)
        if due > now:
            self.sleep(due - now)

    def get_json(self, url):
        host = re.match(r"^https?://([^/]+)", url).group(1)
        for attempt in range(self.retries + 1):
            self._wait(host)
            self.requests.append((host, time.monotonic()))
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    ra = e.headers.get("Retry-After")
                    self.sleep(float(ra) if ra and ra.isdigit() else 2.0 * (attempt + 1))
                    continue
                raise


# ------------------------------------------------------------------ sources → the shared `job` record

def clean_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d)>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n\n", s)).strip()


def iso_ts(s):
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return s / 1000.0 if s > 1e11 else float(s)
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


MONEY_RE = re.compile(r"[$€£]\s?\d{2,3}(?:,\d{3}|k|K)(?:\s?(?:-|–|—|to)\s?[$€£]?\s?\d{2,3}(?:,\d{3}|k|K))?")
REMOTE_RE = re.compile(r"\bremote\b", re.I)


def make_job(source, org, pid, company, title, location, url, apply_url, posted_at, description, remote=None,
             country=None, compensation=None):
    desc = description or ""
    if compensation is None:
        m = MONEY_RE.search(desc)
        compensation = m.group(0) if m else None
    return {
        "id": "%s:%s:%s" % (source, org, pid), "source": source, "org": org, "company": company or org,
        "title": (title or "").strip(), "location": location or "", "url": url, "apply_url": apply_url or url,
        "posted_at": posted_at, "description_text": desc,
        "remote": bool(remote) if remote is not None else bool(REMOTE_RE.search("%s %s" % (location, title))),
        "country": country, "compensation": compensation, "role": role_family(title), "seniority": seniority(title),
        "skills": skills_in("%s\n%s" % (title, desc)),
    }


def from_greenhouse(org, data):
    jobs = []
    for j in (data or {}).get("jobs", []):
        jobs.append(make_job("greenhouse", org, j["id"], j.get("company_name"), j.get("title"),
                             (j.get("location") or {}).get("name", ""), j.get("absolute_url"), j.get("absolute_url"),
                             iso_ts(j.get("first_published") or j.get("updated_at")), clean_html(j.get("content"))))
    return jobs


def from_lever(org, data):
    jobs = []
    for j in data or []:
        cat = j.get("categories") or {}
        sal = j.get("salaryRange") or {}
        comp = ("%s %s–%s" % (sal.get("currency", ""), sal.get("min"), sal.get("max"))).strip() if sal.get("min") else None
        desc = "\n".join(filter(None, [j.get("descriptionPlain"), *[
            "%s\n%s" % (l.get("text", ""), clean_html(l.get("content", ""))) for l in j.get("lists") or []], j.get("additionalPlain")]))
        jobs.append(make_job("lever", org, j["id"], org, j.get("text"), cat.get("location", ""),
                             j.get("hostedUrl"), j.get("applyUrl"), iso_ts(j.get("createdAt")), desc,
                             remote=(j.get("workplaceType") == "remote") or None, country=j.get("country"), compensation=comp))
    return jobs


def from_ashby(org, data):
    jobs = []
    for j in (data or {}).get("jobs", []):
        if j.get("isListed") is False:
            continue
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        comp = ((j.get("compensation") or {}).get("compensationTierSummary")) or None
        remote = j.get("isRemote") if j.get("isRemote") is not None else (j.get("workplaceType") == "Remote" or None)
        jobs.append(make_job("ashby", org, j["id"], org, j.get("title"), j.get("location", ""),
                             j.get("jobUrl"), j.get("applyUrl"), iso_ts(j.get("publishedAt")),
                             j.get("descriptionPlain") or clean_html(j.get("descriptionHtml")), remote=remote,
                             country=addr.get("addressCountry"), compensation=comp))
    return jobs


def from_remoteok(data):
    jobs = []
    for j in (data or [])[1:]:  # the first element is a legal notice
        if not isinstance(j, dict) or "id" not in j:
            continue
        comp = "$%s–$%s" % (j["salary_min"], j["salary_max"]) if j.get("salary_min") else None
        jobs.append(make_job("remoteok", "remoteok", j["id"], j.get("company"), j.get("position"), j.get("location") or "Remote",
                             j.get("url"), j.get("apply_url") or j.get("url"), iso_ts(j.get("epoch")), clean_html(j.get("description")),
                             remote=True, compensation=comp))
    return jobs


PARSERS = {"greenhouse": from_greenhouse, "lever": from_lever, "ashby": from_ashby}
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def strip_boilerplate(jobs, share=0.5, min_jobs=5):
    """Re-extract skills without the company's standard text. A sentence in at least `share` of one company's
    postings is its About/benefits blurb, not the job: Anduril's blurb names "computer vision", which made every
    one of its 450 engineering postings look like a computer-vision job."""
    if len(jobs) < min_jobs:
        return jobs
    counts = {}
    for j in jobs:
        for sent in {x.strip() for x in SENTENCE_RE.split(j["description_text"]) if len(x.strip()) > 30}:
            counts[sent] = counts.get(sent, 0) + 1
    common = {x for x, c in counts.items() if c >= share * len(jobs)}
    if not common:
        return jobs
    for j in jobs:
        own = " ".join(x for x in SENTENCE_RE.split(j["description_text"]) if x.strip() not in common)
        j["skills"] = skills_in("%s\n%s" % (j["title"], own))
    return jobs


def fetch_org(http, source, org):
    data = http.get_json(URLS[source].format(org=org))
    return None if data is None else strip_boilerplate(PARSERS[source](org, data))


BOARD_RX = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    "lever": re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
}


def orgs_in_text(text):
    text = html.unescape(text or "")
    return {k: {m.lower().rstrip(".") for m in rx.findall(text)} for k, rx in BOARD_RX.items()}


def discover_hn(http, months=3):
    """Job-board slugs linked from the last `months` HN 'Who is hiring?' threads."""
    hits = (http.get_json(URLS["hn_threads"]) or {}).get("hits", [])
    threads = [h for h in hits if "who is hiring" in h.get("title", "").lower()][:months]
    found = {k: set() for k in BOARD_RX}

    def walk(node):
        for k, v in orgs_in_text(node.get("text")).items():
            found[k] |= v
        for c in node.get("children") or []:
            walk(c)
    for t in threads:
        walk(http.get_json(URLS["hn_item"].format(id=t["objectID"])) or {})
    return {k: sorted(v) for k, v in found.items()}, [t["title"] for t in threads]


# ------------------------------------------------------------------ store

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, source TEXT, org TEXT, company TEXT, title TEXT, location TEXT,
  remote INTEGER, country TEXT, url TEXT, apply_url TEXT, posted_at REAL, description_text TEXT, compensation TEXT,
  role TEXT, seniority TEXT, skills TEXT, first_seen REAL, last_seen REAL, closed_at REAL);
CREATE TABLE IF NOT EXISTS runs (ts REAL, source TEXT, org TEXT, ok INTEGER, n INTEGER);
CREATE INDEX IF NOT EXISTS jobs_org ON jobs(source, org);
"""
COLS = ("id", "source", "org", "company", "title", "location", "remote", "country", "url", "apply_url", "posted_at",
        "description_text", "compensation", "role", "seniority", "skills")


def open_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def save_org(con, source, org, jobs, now):
    """Upsert this org's postings. A posting missing from a *successful* fetch is closed; a failed fetch closes nothing."""
    ids = set()
    for j in jobs:
        ids.add(j["id"])
        row = [j[c] if c != "skills" else json.dumps(j["skills"]) for c in COLS]
        con.execute("INSERT INTO jobs (%s, first_seen, last_seen, closed_at) VALUES (%s, ?, ?, NULL) "
                    "ON CONFLICT(id) DO UPDATE SET %s, last_seen=excluded.last_seen, closed_at=NULL" % (
                        ", ".join(COLS), ", ".join("?" * len(COLS)), ", ".join("%s=excluded.%s" % (c, c) for c in COLS[1:])),
                    row + [now, now])
    open_ids = {r[0] for r in con.execute("SELECT id FROM jobs WHERE source=? AND org=? AND closed_at IS NULL", (source, org))}
    for gone in open_ids - ids:
        con.execute("UPDATE jobs SET closed_at=? WHERE id=?", (now, gone))
    con.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (now, source, org, 1, len(jobs)))


def load_orgs(paths):
    orgs = {k: set() for k in PARSERS}
    for p in paths:
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    if k in orgs:
                        orgs[k] |= set(v)
    return {k: sorted(v) for k, v in orgs.items()}


def fetch_all(con, http, orgs, now, remoteok=True, log=None):
    """One worker per source host, so each host sees one polite request at a time."""
    lock = threading.Lock()
    summary = {"orgs_ok": 0, "orgs_failed": 0, "postings": 0}

    def work(source, names):
        for org in names:
            err = None
            try:
                jobs = fetch_org(http, source, org)
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
                jobs, err = None, e
            with lock:
                if jobs is None:
                    summary["orgs_failed"] += 1
                    con.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (now, source, org, 0, 0))
                    if log:
                        log("  %s/%s: %s" % (source, org, err or "no public board"))
                else:
                    summary["orgs_ok"] += 1
                    summary["postings"] += len(jobs)
                    save_org(con, source, org, jobs, now)

    with ThreadPoolExecutor(max_workers=len(PARSERS) + 1) as ex:
        futures = [ex.submit(work, s, n) for s, n in orgs.items() if n]
        if remoteok:
            futures.append(ex.submit(_remoteok, con, http, now, lock, summary))
        for f in futures:
            f.result()
    con.commit()
    return summary


def _remoteok(con, http, now, lock, summary):
    try:
        jobs = from_remoteok(http.get_json(URLS["remoteok"]))
    except (urllib.error.URLError, OSError, ValueError):
        jobs = None
    with lock:
        if jobs is None:
            summary["orgs_failed"] += 1
        else:
            summary["orgs_ok"] += 1
            summary["postings"] += len(jobs)
            save_org(con, "remoteok", "remoteok", jobs, now)


# ------------------------------------------------------------------ trends

def _rows(con):
    rows = [dict(r) for r in con.execute("SELECT id, org, company, title, role, seniority, skills, posted_at, first_seen, "
                                         "closed_at, remote, location, url, apply_url, compensation FROM jobs")]
    for r in rows:
        r["skills"] = set(json.loads(r["skills"] or "[]"))
    return rows


def windows(con, now):
    """Two sets of postings to compare, and how they were chosen.
    history: postings *open* now vs open 4 weeks ago, from our own snapshots (unbiased; needs 4 weeks of history).
    posted:  postings *published* in the last 4 weeks vs the 4 weeks before, from the boards' dates. Filled
             postings disappear from the boards, so the older window is undercounted: shares are usable,
             counts are not."""
    rows = _rows(con)
    first_run = con.execute("SELECT MIN(ts) FROM runs WHERE ok=1").fetchone()[0]
    then = now - WINDOW * DAY
    if first_run is not None and first_run <= then:
        is_open = lambda r, t: r["first_seen"] <= t and (r["closed_at"] is None or r["closed_at"] > t)  # noqa: E731
        return "history", [r for r in rows if is_open(r, now)], [r for r in rows if is_open(r, then)], rows
    recent = [r for r in rows if r["posted_at"] and then <= r["posted_at"] <= now and r["closed_at"] is None]
    before = [r for r in rows if r["posted_at"] and then - WINDOW * DAY <= r["posted_at"] < then and r["closed_at"] is None]
    return "posted", recent, before, rows


def capped(rows, cap=None):
    """At most `cap` postings per company, chosen deterministically (by id hash), so the result is reproducible."""
    cap = PER_COMPANY if cap is None else cap
    by = {}
    for r in sorted(rows, key=lambda r: zlib.crc32(r["id"].encode())):
        by.setdefault(r["company"], []).append(r)
    return [r for rs in by.values() for r in rs[:cap]]


def skill_trends(now_rows, then_rows, families=ENGINEERING):
    a = capped([r for r in now_rows if r["role"] in families])
    b = capped([r for r in then_rows if r["role"] in families])
    out = {"n_now": len(a), "n_then": len(b), "enough": len(a) >= MIN_TREND and len(b) >= MIN_TREND, "rising": [], "falling": [], "flat": 0}
    if not out["enough"]:
        return out
    # Dozens of skills are tested at once: a skill only counts as rising or falling if its Fisher test
    # survives a Holm correction across all of them. Without it, 1 in 20 flat skills would "move".
    items = []
    for skill in sorted({s for r in a + b for s in r["skills"]}):
        ka, kb = sum(skill in r["skills"] for r in a), sum(skill in r["skills"] for r in b)
        if ka + kb < 10:
            continue
        items.append({"skill": skill, "now": ka / len(a), "then": kb / len(b), "ci": newcombe(ka, len(a), kb, len(b)),
                      "p": fisher_exact(ka, len(a) - ka, kb, len(b) - kb)})
    for item, adj in zip(items, holm([x["p"] for x in items])):
        item["p_holm"] = adj
        if adj < 0.05 and item["now"] > item["then"]:
            out["rising"].append(item)
        elif adj < 0.05:
            out["falling"].append(item)
        else:
            out["flat"] += 1
    out["rising"].sort(key=lambda x: -(x["now"] - x["then"]))
    out["falling"].sort(key=lambda x: x["now"] - x["then"])
    return out


def top_skills(rows, families=ENGINEERING, k=15):
    a = capped([r for r in rows if r["role"] in families])
    counts = {}
    for r in a:
        for s in r["skills"]:
            counts[s] = counts.get(s, 0) + 1
    return [(s, c / len(a), wilson(c, len(a))) for s, c in sorted(counts.items(), key=lambda x: -x[1])[:k]] if a else []


def company_moves(mode, now_rows, then_rows):
    """history mode: companies that at least doubled their open postings (≥ 5 now). posted mode: only the
    companies publishing the most new postings, with no growth claim (the older window is undercounted)."""
    count = lambda rows: {c: sum(1 for r in rows if r["company"] == c) for c in {r["company"] for r in rows}}  # noqa: E731
    now_c, then_c = count(now_rows), count(then_rows)
    if mode == "history":
        ramp = [(c, then_c.get(c, 0), n) for c, n in now_c.items() if n >= 5 and n >= 2 * max(1, then_c.get(c, 0))]
        return {"kind": "ramping", "companies": sorted(ramp, key=lambda x: -x[2])[:25]}
    return {"kind": "most-new", "companies": sorted(((c, None, n) for c, n in now_c.items()), key=lambda x: -x[2])[:25]}


TITLE_NOISE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|junior|jr\.?|intern|i{1,3}|iv|[0-9]+|remote|hybrid|"
                         r"us|usa|emea|apac|contract|full[- ]time|part[- ]time)\b|\(.*?\)|[,\-–—/|:].*$", re.I)


def norm_title(t):
    return re.sub(r"\s+", " ", TITLE_NOISE.sub(" ", (t or "").lower())).strip()


def new_titles(rows, now_rows, now):
    """Engineering title families with ≥ 3 open postings at ≥ 2 companies, none published before the window."""
    then = now - WINDOW * DAY
    older = {norm_title(r["title"]) for r in rows if (r["posted_at"] or r["first_seen"]) < then}
    groups = {}
    for r in (r for r in now_rows if r["role"] in ENGINEERING):
        groups.setdefault(norm_title(r["title"]), []).append(r)
    out = [(t, len(rs), len({r["company"] for r in rs})) for t, rs in groups.items()
           if t and t not in older and len(rs) >= 3 and len({r["company"] for r in rs}) >= 2]
    return sorted(out, key=lambda x: -x[1])[:15]


def trends(con, now):
    mode, now_rows, then_rows, rows = windows(con, now)
    companies = {r["company"] for r in rows if r["closed_at"] is None}
    return {"mode": mode, "now": now, "open_postings": sum(1 for r in rows if r["closed_at"] is None),
            "companies": len(companies), "sources": sorted({r["id"].split(":")[0] for r in rows}),
            "skills": skill_trends(now_rows, then_rows), "top": top_skills([r for r in rows if r["closed_at"] is None]),
            "moves": company_moves(mode, now_rows, then_rows), "new_titles": new_titles(rows, now_rows, now),
            "families": {f: sum(1 for r in now_rows if r["role"] == f) for f in sorted({r["role"] for r in now_rows})}}


# ------------------------------------------------------------------ personal fit

WEIGHTS = {"skills": 0.5, "role": 0.25, "level": 0.15, "fresh": 0.10}


def resume_skills(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".json"):
        text = json.dumps(json.loads(text))
    return set(skills_in(text))


def wanted_families(roles):
    fams = set()
    for r in roles:
        f = role_family(r if re.search(r"engineer|developer|scientist", r, re.I) else r + " engineer")
        fams.add(f)
    return fams


def score(job, mine, fams, level, now):
    js = job["skills"]
    # out of at least 3: a posting naming one skill you have is not a "100% match"
    cover = len(js & mine) / max(len(js), 3)
    role = 1.0 if job["role"] in fams else 0.0
    if level is None or job["seniority"] == "unspecified":
        lv = 0.5
    else:
        dist = abs(LEVEL_ORDER.index(level) - LEVEL_ORDER.index(job["seniority"])) if job["seniority"] in LEVEL_ORDER else 2
        lv = {0: 1.0, 1: 0.5}.get(dist, 0.0)
    age = (now - (job["posted_at"] or job["first_seen"])) / DAY
    fresh = 1.0 if age <= 14 else 0.5 if age <= 45 else 0.0
    parts = {"skills": cover, "role": role, "level": lv, "fresh": fresh}
    return sum(WEIGHTS[k] * v for k, v in parts.items()), parts


# "Remote" is usually remote *within a region*: "Remote - US" means living in the US. These decide whether a
# remote posting is open to someone living in a given country.
REGIONS = {"india": ["india", "apac", "asia", "asia pacific", "south asia"], "united kingdom": ["uk", "united kingdom", "emea", "europe"],
           "united states": ["us", "u.s.", "usa", "united states", "americas", "north america"], "canada": ["canada", "americas", "north america"],
           "germany": ["germany", "emea", "europe", "eu"], "singapore": ["singapore", "apac", "asia"]}
PLACES = sorted({p for ps in REGIONS.values() for p in ps} | {"latam", "mexico", "brazil", "france", "spain", "netherlands", "ireland",
                 "poland", "portugal", "australia", "japan", "israel", "switzerland", "sweden", "philippines"}, key=len, reverse=True)
ANYWHERE_RE = re.compile(r"\b(anywhere|worldwide|world-wide|global(ly)?|fully remote|remote[- ]first|any location)\b", re.I)
NOT_REMOTE_RE = re.compile(r"\bhybrid\b|\bin[- ]office\b|\bon-?site\b", re.I)
RESTRICTED_RE = re.compile(r"(must|need to|required to|should)( currently)? (be )?(be )?(located|based|reside|living|live)[^.]{0,50}?\b"
                           r"(the )?(us|u\.s\.|usa|united states|canada|uk|united kingdom|europe|eu|emea)\b|\b(us|u\.s\.)[- ]based\b|"
                           r"(authori[sz]ed|eligible) to work in the (united states|us|u\.s\.|uk|united kingdom|canada)\b|"
                           r"\b(us|u\.s\.|uk|canada|eu) (time ?zones?|residents?|citizens?) only\b", re.I)


def remote_open_to(job, country, description=""):
    """'yes', 'check' (plain "Remote" with no restriction found) or 'no' for someone living in `country`."""
    loc = (job.get("location") or "").lower()
    if NOT_REMOTE_RE.search(loc) or not (job.get("remote") or "remote" in loc):
        return "no"
    mine = REGIONS.get(country.lower(), [country.lower()])
    if any(re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(p), loc) for p in mine) or ANYWHERE_RE.search(loc):
        return "yes"
    if any(re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(p), loc) for p in PLACES):
        return "no"  # remote, but tied to somewhere else
    if RESTRICTED_RE.search(description or ""):
        return "no"
    return "check"


def match(con, mine, roles, now, level=None, remote=False, location=None, top=20, remote_from=None):
    fams = wanted_families(roles)
    rows = [r for r in _rows(con) if r["closed_at"] is None]
    if remote and remote_from:
        cands = rows
        descs = {}
        plain = [r["id"] for r in cands if not NOT_REMOTE_RE.search(r["location"] or "") and (r["remote"] or "remote" in (r["location"] or "").lower())]
        for i in range(0, len(plain), 500):
            chunk = plain[i:i + 500]
            descs.update(dict(con.execute("SELECT id, description_text FROM jobs WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk)))
        kept = []
        for r in cands:
            ok = remote_open_to(r, remote_from, descs.get(r["id"], ""))
            if ok != "no":
                kept.append(dict(r, remote_ok=ok))
        rows = kept
    elif remote:
        rows = [r for r in rows if r["remote"]]
    if location:
        rows = [r for r in rows if r["remote"] or location.lower() in (r["location"] or "").lower()]
    scored = []
    for r in rows:
        s, parts = score(r, mine, fams, level, now)
        scored.append(dict(r, score=s, parts=parts, matched=sorted(r["skills"] & mine), missing=sorted(r["skills"] - mine)))
    scored.sort(key=lambda x: -x["score"])
    ranked = [x for x in scored if x["role"] in fams][:top]
    return {"families": sorted(fams), "ranked": ranked, "adjacent": adjacent(scored, fams, con, now)}


def adjacent(scored, fams, con, now):
    """Role families you didn't list whose postings cover your skills at least as well as the ones you did."""
    by = {}
    for x in scored:
        if x["role"] in ENGINEERING and x["skills"]:
            by.setdefault(x["role"], []).append(x["parts"]["skills"])
    mine = [c for f in fams for c in by.get(f, [])]
    base = median(mine) if mine else 0.0
    _, now_rows, then_rows, _ = windows(con, now)
    out = []
    for f, covs in by.items():
        if f in fams or len(covs) < 10:
            continue
        m = median(covs)
        if m >= max(base, 0.3):
            n_now, n_then = sum(r["role"] == f for r in now_rows), sum(r["role"] == f for r in then_rows)
            out.append({"family": f, "median_cover": m, "open": len(covs), "now": n_now, "then": n_then})
    return sorted(out, key=lambda x: -x["median_cover"])


# ------------------------------------------------------------------ output

def pct(x):
    return "%.0f%%" % (100 * x)


def when(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


MODE_NOTE = {
    "history": "Compared: postings open now vs postings open 4 weeks ago, from whatshiring's own daily snapshots.",
    "posted": ("Compared: postings published in the last 4 weeks vs the 4 weeks before, by the boards' own dates. Filled "
               "postings disappear from boards, so the older window is undercounted; skill *shares* are compared, not counts, "
               "and no company-growth claims are made until 4 weeks of snapshots exist."),
}


def report_trends(t, out):
    out.write("%d open postings at %d companies (%s), %s.\n%s\n" % (
        t["open_postings"], t["companies"], ", ".join(t["sources"]), when(t["now"]), MODE_NOTE[t["mode"]]))
    s = t["skills"]
    out.write("\nSkills in engineering postings (%d now vs %d then; at most %d per company, so no single employer sets the trend):\n" % (
        s["n_now"], s["n_then"], PER_COMPANY))
    if not s["enough"]:
        out.write("  not enough postings for a trend (need %d on each side)\n" % MIN_TREND)
    else:
        for label, items in (("Rising", s["rising"]), ("Falling", s["falling"])):
            out.write("  %s:%s\n" % (label, "" if items else " none detectable"))
            for x in items[:12]:
                out.write("    %-22s %5s → %5s  (difference 95%% CI %+.1f…%+.1f pts, Holm-adjusted p=%.2g)\n" % (
                    x["skill"], pct(x["then"]), pct(x["now"]), 100 * x["ci"][0], 100 * x["ci"][1], x["p_holm"]))
        out.write("  %d other skills: no detectable change after correcting for testing them all at once\n" % s["flat"])
    if t["top"]:
        out.write("\nMost-requested skills in open engineering postings:\n")
        for name, share, ci in t["top"][:10]:
            out.write("  %-22s %5s (%s–%s)\n" % (name, pct(share), pct(ci[0]), pct(ci[1])))
    mv = t["moves"]
    if mv["companies"]:
        out.write("\n%s\n" % ("Companies that at least doubled their open postings:" if mv["kind"] == "ramping"
                               else "Most new postings in the last 4 weeks (a count, not a growth claim):"))
        for c, then, n in mv["companies"][:10]:
            out.write("  %-28s %s\n" % (c, "%d → %d" % (then, n) if then is not None else n))
    if t["new_titles"]:
        out.write("\nNew titles (not seen before this window):\n")
        for title, n, companies in t["new_titles"][:8]:
            out.write("  %-40s %d postings at %d companies\n" % (title, n, companies))


def report_match(m, out):
    out.write("Roles: %s\n\n" % ", ".join(m["families"]))
    for x in m["ranked"]:
        p = x["parts"]
        out.write("%.2f  %s — %s (%s)%s\n      skills %.0f%% · role %.0f · level %.1f · fresh %.1f   %s\n" % (
            x["score"], x["company"], x["title"], x["location"] or "?", "  " + x["compensation"] if x["compensation"] else "",
            100 * p["skills"], p["role"], p["level"], p["fresh"], x["apply_url"] or x["url"]))
        if x["missing"]:
            out.write("      you don't list: %s\n" % ", ".join(x["missing"][:8]))
    if m["adjacent"]:
        out.write("\nRoles you didn't list that fit your skills as well:\n")
        for a in m["adjacent"]:
            out.write("  %-12s your skills cover a median %.0f%% of each posting's skills; %d open (%d new in 4 weeks)\n" % (
                a["family"], 100 * a["median_cover"], a["open"], a["now"]))


def svg_bars(items, width=640, row=22):
    """Diverging bars: change in share (pts) with its 95% interval."""
    if not items:
        return ""
    lim = max(max(abs(x["ci"][0]), abs(x["ci"][1])) for x in items) or 0.01
    h = 20 + row * len(items)
    mid = 330
    sx = lambda v: mid + (width - mid - 20) * v / lim  # noqa: E731
    p = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" role="img" aria-label="Change in share of postings">' % (width, h),
         '<line x1="%d" y1="0" x2="%d" y2="%d" stroke="currentColor" stroke-opacity=".4"/>' % (mid, mid, h)]
    for i, x in enumerate(items):
        y = 10 + i * row
        d = x["now"] - x["then"]
        color = "#2b8a3e" if d > 0 else "#c92a2a"
        p.append('<text x="%d" y="%d" font-size="12" text-anchor="end" fill="currentColor">%s</text>' % (mid - 110, y + 12, html.escape(x["skill"])))
        p.append('<text x="%d" y="%d" font-size="11" text-anchor="end" fill="currentColor" fill-opacity=".7">%s → %s</text>' % (mid - 8, y + 12, pct(x["then"]), pct(x["now"])))
        p.append('<rect x="%.1f" y="%d" width="%.1f" height="12" fill="%s" fill-opacity=".8"/>' % (min(mid, sx(d)), y + 2, abs(sx(d) - mid), color))
        p.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="currentColor"/>' % (sx(x["ci"][0]), y + 8, sx(x["ci"][1]), y + 8))
    p.append("</svg>")
    return "".join(p)


def html_report(t):
    body = []
    report_trends(t, type("W", (), {"write": lambda self, s: body.append(s)})())
    s = t["skills"]
    chart = svg_bars((s["rising"][:10] + s["falling"][:10]) if s["enough"] else [])
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>What's hiring: %s</title><style>body{font:15px system-ui;max-width:800px;margin:40px auto;padding:0 16px;"
            "color:#1a1a1a;background:#fff}pre{white-space:pre-wrap}svg{max-width:100%%;height:auto}"
            "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}}</style>"
            "<h1>What's hiring, week of %s</h1><p>%d open postings at %d companies, from public job boards.</p>%s<pre>%s</pre>"
            "<p><small>Made with <a href='https://github.com/sandeepsirodia/whatshiring'>whatshiring</a>. Run it yourself to rank "
            "these postings against your own resume, locally.</small></p>") % (
        when(t["now"]), when(t["now"]), t["open_postings"], t["companies"], chart, html.escape("".join(body)))


def export_snapshot(con, path):
    """A compact snapshot (no descriptions) so the weekly job can carry history between runs."""
    rows = [dict(r) for r in con.execute("SELECT id, source, org, company, title, location, remote, country, url, apply_url, "
                                         "posted_at, compensation, role, seniority, skills, first_seen, last_seen, closed_at FROM jobs")]
    runs = [list(r) for r in con.execute("SELECT * FROM runs WHERE ok=1 GROUP BY ts, source")]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "jobs": rows, "runs": runs}, f)


def import_snapshot(con, path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for r in data["jobs"]:
        r.setdefault("description_text", "")
        cols = list(r)
        con.execute("INSERT OR IGNORE INTO jobs (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))), [r[c] for c in cols])
    for run in data["runs"]:
        con.execute("INSERT INTO runs VALUES (?,?,?,?,?)", run)
    con.commit()
    return len(data["jobs"])


# ------------------------------------------------------------------ CLI

def main(argv=None, out=None, http=None):
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="whatshiring", description="What's actually hiring this week, what's rising, and what fits you.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--now", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="snapshot every job board in the org lists")
    f.add_argument("--orgs", action="append", default=[], help="extra orgs.json (the bundled list is always used)")
    f.add_argument("--no-remoteok", action="store_true")
    d = sub.add_parser("discover", help="find company boards linked from recent HN 'Who is hiring?' threads")
    d.add_argument("--months", type=int, default=3)
    d.add_argument("--out", default=os.path.join(os.path.expanduser("~"), ".applyloop", "orgs.json"))
    t = sub.add_parser("trends", help="rising and falling skills, companies, new titles")
    t.add_argument("--json", action="store_true")
    r = sub.add_parser("report", help="the weekly HTML report")
    r.add_argument("--html", required=True)
    r.add_argument("--snapshot-out", help="also write a compact snapshot (no descriptions) for the next run")
    i = sub.add_parser("import", help="load a snapshot written by report --snapshot-out")
    i.add_argument("path")
    m = sub.add_parser("match", help="rank open postings against your resume")
    m.add_argument("--resume", required=True, help="JSON Resume, Markdown or plain text")
    m.add_argument("--roles", required=True, help='comma-separated, e.g. "backend, platform"')
    m.add_argument("--level", choices=LEVEL_ORDER)
    m.add_argument("--remote", action="store_true", help="remote postings only")
    m.add_argument("--remote-from", metavar="COUNTRY", help='with --remote: only remote postings open to someone living there, e.g. "India"')
    m.add_argument("--location", help="keep postings in this location (remote ones always kept)")
    m.add_argument("--top", type=int, default=20)
    m.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    now = datetime.fromisoformat(a.now).replace(tzinfo=timezone.utc).timestamp() if a.now else time.time()
    http = http or Http()
    con = open_db(a.db)
    try:
        if a.cmd == "fetch":
            orgs = load_orgs([os.path.join(HERE, "orgs.json"), os.path.join(os.path.dirname(a.db), "orgs.json")] + a.orgs)
            n = sum(len(v) for v in orgs.values())
            out.write("Fetching %d company boards%s (one request per second per host)…\n" % (n, " + RemoteOK" if not a.no_remoteok else ""))
            s = fetch_all(con, http, orgs, now, remoteok=not a.no_remoteok)
            out.write("%(postings)d open postings from %(orgs_ok)d boards (%(orgs_failed)d had no public board)\n" % s)
        elif a.cmd == "discover":
            found, threads = discover_hn(http, a.months)
            known = load_orgs([a.out]) if os.path.exists(a.out) else {k: [] for k in PARSERS}
            merged = {k: sorted(set(known.get(k, [])) | set(found[k])) for k in PARSERS}
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with open(a.out, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, indent=1)
            out.write("From %s: %s. Saved to %s\n" % ("; ".join(threads), ", ".join("%d %s" % (len(v), k) for k, v in found.items()), a.out))
        elif a.cmd == "trends":
            res = trends(con, now)
            if a.json:
                out.write(json.dumps(res, indent=2, default=list) + "\n")
            else:
                report_trends(res, out)
        elif a.cmd == "report":
            res = trends(con, now)
            with open(a.html, "w", encoding="utf-8") as fh:
                fh.write(html_report(res))
            if a.snapshot_out:
                export_snapshot(con, a.snapshot_out)
            out.write("Wrote %s\n" % a.html)
        elif a.cmd == "import":
            out.write("Imported %d postings\n" % import_snapshot(con, a.path))
        elif a.cmd == "match":
            res = match(con, resume_skills(a.resume), [x.strip() for x in a.roles.split(",") if x.strip()], now,
                        a.level, a.remote, a.location, a.top, a.remote_from)
            if a.json:
                out.write(json.dumps(res, indent=2, default=list) + "\n")
            else:
                report_match(res, out)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
