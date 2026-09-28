"""Tests map 1:1 to SPEC.md (E1..E10). Fixtures are trimmed copies of the real API shapes (Sept 2026)."""
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import whatshiring as w  # noqa: E402

DAY = 86400
NOW = 1_790_000_000.0

GREENHOUSE = {"jobs": [{
    "id": 111, "title": "Senior Backend Engineer", "company_name": "Acme",
    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/111", "location": {"name": "Remote - US"},
    "first_published": "2026-09-01T10:00:00-04:00", "updated_at": "2026-09-20T10:00:00-04:00",
    "content": "&lt;p&gt;We use &lt;strong&gt;Go&lt;/strong&gt;, PostgreSQL and k8s.&lt;/p&gt;&lt;p&gt;Pay: $180,000 - $220,000&lt;/p&gt;"}]}
LEVER = [{"id": "abc-1", "text": "Frontend Engineer", "categories": {"location": "Berlin"}, "country": "DE",
          "workplaceType": "hybrid", "createdAt": 1788000000000, "hostedUrl": "https://jobs.lever.co/globex/abc-1",
          "applyUrl": "https://jobs.lever.co/globex/abc-1/apply", "descriptionPlain": "React and TypeScript.",
          "lists": [{"text": "You have", "content": "<li>Next.js</li>"}], "additionalPlain": ""}]
ASHBY = {"jobs": [{"id": "z-9", "title": "ML Engineer, Inference", "location": "San Francisco", "isRemote": False,
                   "publishedAt": "2026-09-10T16:38:15.322+00:00", "isListed": True, "jobUrl": "https://jobs.ashbyhq.com/initech/z-9",
                   "applyUrl": "https://jobs.ashbyhq.com/initech/z-9/application", "descriptionPlain": "PyTorch, CUDA, vLLM.",
                   "address": {"postalAddress": {"addressCountry": "United States"}},
                   "compensation": {"compensationTierSummary": "$250K – $300K"}},
                  {"id": "z-10", "title": "Hidden", "isListed": False, "descriptionPlain": ""}]}
REMOTEOK = [{"legal": "notice"}, {"id": 5, "position": "Rust Developer", "company": "Hooli", "epoch": 1789000000,
                                  "url": "https://remoteok.com/l/5", "description": "<p>Rust &amp; Tokio</p>", "salary_min": 100000, "salary_max": 150000}]
HN_THREADS = {"hits": [{"title": "Ask HN: Who is hiring? (September 2026)", "objectID": "900"},
                       {"title": "Ask HN: Who wants to be hired? (September 2026)", "objectID": "901"}]}
HN_ITEM = {"text": "", "children": [
    {"text": "Acme | Remote | https:&#x2F;&#x2F;boards.greenhouse.io&#x2F;Acme&#x2F;jobs&#x2F;1", "children": [
        {"text": "also https://job-boards.greenhouse.io/acme and https://jobs.lever.co/globex/abc", "children": []}]},
    {"text": "Initech: jobs.ashbyhq.com&#x2F;initech.", "children": []}]}


class FakeBoards(http.server.BaseHTTPRequestHandler):
    routes, hits, throttle_once = {}, [], set()

    def do_GET(self):
        FakeBoards.hits.append(self.path)
        if self.path in FakeBoards.throttle_once:
            FakeBoards.throttle_once.discard(self.path)
            self.send_response(429)
            self.send_header("Retry-After", "2")
            self.end_headers()
            return
        body = FakeBoards.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def job(role="backend", skills=(), posted=NOW - 5 * DAY, company="C", title=None, seniority="unspecified", i=[0]):
    i[0] += 1
    return {"id": "t:%s:%d" % (company, i[0]), "source": "t", "org": company, "company": company,
            "title": title or "%s engineer" % role, "location": "Remote", "url": "u", "apply_url": "u", "posted_at": posted,
            "description_text": "", "remote": True, "country": None, "compensation": None, "role": role,
            "seniority": seniority, "skills": sorted(skills)}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakeBoards)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % cls.server.server_port
        cls.saved = dict(w.URLS)
        w.URLS.update({"greenhouse": base + "/gh/{org}", "lever": base + "/lever/{org}", "ashby": base + "/ashby/{org}",
                       "remoteok": base + "/remoteok", "hn_threads": base + "/hn", "hn_item": base + "/hn/{id}"})
        FakeBoards.routes = {"/gh/acme": GREENHOUSE, "/lever/globex": LEVER, "/ashby/initech": ASHBY,
                             "/remoteok": REMOTEOK, "/hn": HN_THREADS, "/hn/900": HN_ITEM}

    @classmethod
    def tearDownClass(cls):
        w.URLS.clear()
        w.URLS.update(cls.saved)
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeBoards.hits.clear()
        self.dir = tempfile.mkdtemp(prefix="whatshiring-")
        self.con = w.open_db(os.path.join(self.dir, "jobs.sqlite"))
        self.addCleanup(self.con.close)
        self.sleeps = []
        self.http = w.Http(gap=0, sleep=self.sleeps.append)


class TestSources(Base):
    def test_e1_each_board_maps_to_the_shared_job_record(self):
        gh = w.fetch_org(self.http, "greenhouse", "acme")[0]
        self.assertEqual((gh["id"], gh["company"], gh["role"], gh["seniority"], gh["remote"]),
                         ("greenhouse:acme:111", "Acme", "backend", "senior", True))
        self.assertEqual(gh["description_text"], "We use Go , PostgreSQL and k8s.\n Pay: $180,000 - $220,000")
        self.assertEqual(gh["compensation"], "$180,000 - $220,000")
        self.assertEqual(gh["skills"], ["Go", "Kubernetes", "PostgreSQL"])
        self.assertAlmostEqual(gh["posted_at"], 1788271200.0)
        lv = w.fetch_org(self.http, "lever", "globex")[0]
        self.assertEqual((lv["role"], lv["country"], lv["remote"], lv["posted_at"]), ("frontend", "DE", False, 1788000000.0))
        self.assertEqual(lv["skills"], ["Next.js", "React", "TypeScript"])
        ash = w.fetch_org(self.http, "ashby", "initech")
        self.assertEqual(len(ash), 1, "unlisted postings are skipped")
        self.assertEqual((ash[0]["role"], ash[0]["compensation"], ash[0]["country"]), ("ml/ai", "$250K – $300K", "United States"))
        ro = w.from_remoteok(REMOTEOK)[0]
        self.assertEqual((ro["company"], ro["remote"], ro["compensation"], ro["skills"]), ("Hooli", True, "$100000–$150000", ["Rust"]))
        self.assertIsNone(w.fetch_org(self.http, "greenhouse", "no-such-org"), "404 means no public board")

    def test_e2_hn_threads_give_deduplicated_board_slugs(self):
        found, threads = w.discover_hn(self.http, months=3)
        self.assertEqual(threads, ["Ask HN: Who is hiring? (September 2026)"])
        self.assertEqual(found, {"greenhouse": ["acme"], "lever": ["globex"], "ashby": ["initech"]})

    def test_e3_snapshots_close_postings_that_disappear(self):
        a, b = job(company="acme"), job(company="acme")
        w.save_org(self.con, "t", "acme", [a, b], NOW - 2 * DAY)
        w.save_org(self.con, "t", "acme", [a], NOW)
        rows = {r["id"]: dict(r) for r in self.con.execute("SELECT * FROM jobs")}
        self.assertIsNone(rows[a["id"]]["closed_at"])
        self.assertEqual(rows[b["id"]]["closed_at"], NOW)
        self.assertEqual(rows[a["id"]]["first_seen"], NOW - 2 * DAY)

    def test_failed_fetch_closes_nothing(self):
        w.save_org(self.con, "greenhouse", "acme", w.fetch_org(self.http, "greenhouse", "acme"), NOW - DAY)
        FakeBoards.routes.pop("/gh/acme")
        try:
            w.fetch_all(self.con, self.http, {"greenhouse": ["acme"], "lever": [], "ashby": []}, NOW, remoteok=False)
        finally:
            FakeBoards.routes["/gh/acme"] = GREENHOUSE
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL").fetchone()[0], 1)

    def test_e9_retry_after_is_honoured_and_hosts_are_paced(self):
        FakeBoards.throttle_once.add("/gh/acme")
        jobs = w.fetch_org(self.http, "greenhouse", "acme")
        self.assertEqual(len(jobs), 1)
        self.assertIn(2.0, self.sleeps)
        paced = w.Http(gap=0.05)  # real clock: requests to one host are at least `gap` apart
        for _ in range(3):
            paced.get_json(w.URLS["greenhouse"].format(org="acme"))
        times = [t for _, t in paced.requests]
        self.assertTrue(all(b - a >= 0.049 for a, b in zip(times, times[1:])), times)


class TestSkillsAndStats(unittest.TestCase):
    def test_role_families_from_titles(self):
        for title, fam in [("Forward Deployed Engineer, AI", "fde"), ("Solutions Engineer", "fde"), ("Senior AI Engineer", "ml/ai"),
                           ("Full Stack Engineer", "fullstack"), ("Frontend Engineer", "frontend"), ("Software Engineer, Backend", "backend")]:
            self.assertEqual(w.role_family(title), fam, title)
        self.assertEqual(w.wanted_families(["Forward deployed engineer", "AI engineer", "full stack engineer", "frontend"]),
                         {"fde", "ml/ai", "fullstack", "frontend"})

    def test_remote_means_remote_from_where_you_live(self):
        R = lambda loc, remote=True, desc="": w.remote_open_to({"location": loc, "remote": remote}, "India", desc)  # noqa: E731
        self.assertEqual(R("Remote - India"), "yes")
        self.assertEqual(R("Remote, APAC"), "yes")
        self.assertEqual(R("Remote (Worldwide)"), "yes")
        self.assertEqual(R("Remote - US"), "no")
        self.assertEqual(R("Remote, UK"), "no")
        self.assertEqual(R("Remote - Canada"), "no")
        self.assertEqual(R("Hybrid - Bengaluru"), "no")
        self.assertEqual(R("Bengaluru, India", remote=False), "no", "in-office in India is not remote")
        self.assertEqual(R("Remote"), "check")
        self.assertEqual(R("Remote", desc="You must be located in the United States."), "no")
        self.assertEqual(R("Remote", desc="Candidates must be eligible to work in the US."), "no")

    def test_e4_aliases_and_whole_words(self):
        self.assertEqual(w.skills_in("k8s, Kubernetes (EKS)"), ["Kubernetes"])
        self.assertEqual(w.skills_in("Kubernetesque vibes"), [])
        self.assertEqual(w.skills_in("We write Go, Python"), ["Go", "Python"])
        self.assertEqual(w.skills_in("Go beyond. Go to market."), [])
        self.assertEqual(w.skills_in("C/C++ and Rust"), ["C", "C++", "Rust"])
        self.assertEqual(w.skills_in("React Native apps"), ["React Native"])

    def test_e5_newcombe_matches_the_published_example(self):
        # Newcombe (1998), Statistics in Medicine 17:873, example (a): 56/70 vs 48/80, method 10 → 0.0524 to 0.3339
        lo, hi = w.newcombe(56, 70, 48, 80)
        self.assertEqual((round(lo, 4), round(hi, 4)), (0.0524, 0.3339))

    def test_company_blurb_is_not_a_skill_requirement(self):
        blurb = "Acme brings cutting-edge autonomy, AI, computer vision and networking to the world. "
        jobs = [w.make_job("t", "acme", i, "Acme", "Backend Engineer", "", "u", "u", NOW, blurb + ("We use Rust daily." if i == 0 else "We use Go, daily."))
                for i in range(6)]
        self.assertIn("Computer vision", jobs[0]["skills"])
        w.strip_boilerplate(jobs)
        self.assertEqual(jobs[0]["skills"], ["Rust"])
        self.assertEqual(jobs[1]["skills"], ["Go"])
        few = [w.make_job("t", "tiny", i, "Tiny", "Engineer", "", "u", "u", NOW, blurb) for i in range(3)]
        self.assertIn("Computer vision", w.strip_boilerplate(few)[0]["skills"], "too few postings to call anything boilerplate")

    def test_one_big_hirer_cannot_set_the_trend(self):
        big = [dict(job(company="Big"), skills={"CV"}) for _ in range(500)]
        small = [dict(job(company="s%d" % i), skills=set()) for i in range(100)]
        then = [dict(job(company="t%d" % i), skills=set()) for i in range(120)]
        self.assertEqual(len(w.capped(big + small)), 120)
        t = w.skill_trends(big + small, then)
        self.assertEqual(round(t["rising"][0]["now"], 3), round(20 / 120, 3))

    def test_e6_too_few_postings_no_trend(self):
        rows = [dict(job(skills={"Go"}), skills={"Go"}) for _ in range(30)]
        self.assertFalse(w.skill_trends(rows, rows)["enough"])

    def test_holm_keeps_noise_from_becoming_a_trend(self):
        now = [dict(job(company="c%d" % (i % 20)), skills={"Rust"} if i < 60 else set()) for i in range(200)]
        then = [dict(job(company="c%d" % (i % 20)), skills={"Rust"} if i < 20 else set()) for i in range(200)]
        for i, r in enumerate(now + then):  # 40 flat skills, identical in both windows
            r["skills"] |= {"S%d" % k for k in range(40) if (i + k) % 7 == 0}
        t = w.skill_trends(now, then)
        self.assertEqual([x["skill"] for x in t["rising"]], ["Rust"])
        self.assertEqual(t["falling"], [])


class TestTrendsMatchAndReport(Base):
    def seed(self, jobs, when=NOW):
        by = {}
        for j in jobs:
            by.setdefault(j["org"], []).append(j)
        for org, js in by.items():
            w.save_org(self.con, "t", org, js, when)
        self.con.commit()

    def test_posted_mode_is_used_until_four_weeks_of_history(self):
        self.seed([job() for _ in range(3)])
        mode, *_ = w.windows(self.con, NOW)
        self.assertEqual(mode, "posted")
        self.con.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (NOW - 30 * DAY, "t", "C", 1, 3))
        self.assertEqual(w.windows(self.con, NOW)[0], "history")
        self.assertEqual(w.company_moves("posted", [dict(company="A")] * 3, [])["kind"], "most-new")

    def test_e7_ranks_by_skills_and_explains_the_score(self):
        good = job("backend", {"Go", "PostgreSQL", "Kafka"}, company="good", title="Backend Engineer")
        meh = job("backend", {"Java", "Spring", "Oracle"}, company="meh", title="Backend Engineer")
        fe = job("frontend", {"React", "TypeScript"}, company="fe", title="Frontend Engineer")
        self.seed([good, meh, fe])
        res = w.match(self.con, {"Go", "PostgreSQL", "Kafka"}, ["backend", "frontend"], NOW)
        order = [x["company"] for x in res["ranked"]]
        self.assertEqual(order[0], "good")
        self.assertLess(order.index("good"), order.index("fe"))
        self.assertEqual(res["ranked"][0]["parts"]["skills"], 1.0)
        out = io.StringIO()
        w.report_match(res, out)
        self.assertIn("skills 100% · role 1 · level 0.5 · fresh 1.0", out.getvalue())
        self.assertIn("you don't list: Java, Oracle, Spring", out.getvalue())

    def test_e8_suggests_an_adjacent_role(self):
        infra = [job("infra", {"Kubernetes", "Terraform", "Go", "AWS"}, company="i%d" % i) for i in range(12)]
        front = [job("frontend", {"React", "TypeScript", "CSS"}, company="f%d" % i) for i in range(12)]
        self.seed(infra + front)
        res = w.match(self.con, {"Kubernetes", "Terraform", "Go", "AWS"}, ["frontend"], NOW)
        self.assertEqual([a["family"] for a in res["adjacent"]], ["infra"])
        self.assertEqual(res["adjacent"][0]["median_cover"], 1.0)

    def test_e10_html_report_is_self_contained(self):
        self.seed([job("backend", {"Go"}) for _ in range(60)] + [job("backend", {"Go"}, posted=NOW - 40 * DAY) for _ in range(60)])
        page = w.html_report(w.trends(self.con, NOW))
        self.assertIn("What's hiring", page)
        self.assertEqual(page.count("http"), page.count("http://www.w3.org/2000/svg") + page.count("https://github.com/sandeepsirodia/whatshiring"))

    def test_snapshot_round_trip_keeps_history(self):
        self.seed([job() for _ in range(5)], when=NOW - 40 * DAY)
        path = os.path.join(self.dir, "snap.json")
        w.export_snapshot(self.con, path)
        other = w.open_db(os.path.join(self.dir, "other.sqlite"))
        self.addCleanup(other.close)
        self.assertEqual(w.import_snapshot(other, path), 5)
        self.assertEqual(w.windows(other, NOW)[0], "history")

    def test_cli_fetch_and_trends(self):
        orgs = os.path.join(self.dir, "orgs.json")
        with open(orgs, "w") as f:
            json.dump({"greenhouse": ["acme"], "lever": ["globex"], "ashby": ["initech", "gone"]}, f)
        out = io.StringIO()
        saved = w.HERE
        w.HERE = self.dir  # no bundled list in the test dir
        try:
            w.main(["--db", os.path.join(self.dir, "cli.sqlite"), "fetch", "--orgs", orgs], out, http=self.http)
        finally:
            w.HERE = saved
        self.assertIn("4 open postings from 4 boards (1 had no public board)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
