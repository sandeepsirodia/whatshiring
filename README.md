<h1 align="center">whatshiring</h1>

<p align="center">
  <em>What's actually hiring this week, what's rising, and what fits you.</em>
</p>

<p align="center">
  <a href="https://github.com/sandeepsirodia/whatshiring/actions/workflows/ci.yml"><img src="https://github.com/sandeepsirodia/whatshiring/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://sandeepsirodia.github.io/whatshiring/"><img src="https://img.shields.io/badge/weekly%20report-live-111111?style=flat-square" alt="Weekly report"></a>
  <img src="https://img.shields.io/badge/dependencies-0-111111?style=flat-square" alt="Zero dependencies">
  <img src="https://img.shields.io/badge/license-MIT-111111?style=flat-square" alt="MIT">
</p>

---

Every week someone posts "the hottest skills in tech right now". I pulled **20,741 open postings from 208 companies' public job boards** (Sept 28, 2026) and compared new engineering postings from the last 4 weeks with the 4 weeks before:

| | Counting every mention | Company blurbs removed | …and at most 20 postings per company |
|---|---|---|---|
| Rising | **computer vision** 15% → 21%, C++ | C++, robotics, agile | CI/CD 13% → 19% (borderline, p = 0.048) |
| Falling | MongoDB, Cassandra, Redshift, vector databases | the same four | nothing detectable |

**The "computer vision boom" was one sentence.** A defense company published 450 of the 2,235 new engineering postings, and every one of them carries the same About paragraph: *"bringing cutting-edge autonomy, AI, computer vision, sensor fusion…"*. That single blurb produced 450 of the 475 computer-vision mentions. Remove sentences a company repeats in every posting, stop any one employer from outweighing the rest, and correct for testing 100+ skills at once: **almost every trend disappears.** In four weeks, skill demand barely moves. Most "trending skills" charts are measuring who happened to post a lot that month.

whatshiring does that analysis every Monday and publishes it: **[the weekly report](https://sandeepsirodia.github.io/whatshiring/)**. Run it yourself and it also ranks the open postings against *your* resume, on your machine.

## Try it

```bash
pip install whatshiring
whatshiring fetch                      # ~5 minutes: one polite request per second per job-board host
whatshiring trends
whatshiring match --resume resume.json --roles "backend, platform" --level senior --remote
```

```console
$ whatshiring match --resume resume.json --roles "backend, platform" --level senior
0.94  Robinhood — Senior Software Engineer, AI Security (Bellevue, WA; Menlo Park, CA)  $196,000 — $230,000
      skills 88% · role 1 · level 1.0 · fresh 1.0   https://boards.greenhouse.io/robinhood/jobs/8167546
      you don't list: AI agents
0.90  Reddit — Senior Software Engineer, Core Platform (Remote - United States)  $190,800 — $267,100
      skills 100% · role 1 · level 1.0 · fresh 0.0   https://job-boards.greenhouse.io/reddit/jobs/8022441
```

Every score shows its parts: how much of the posting's skill list you cover, whether it's a role you asked for, seniority fit and freshness. There's no black box to argue with. Postings list skills you don't have ("you don't list: …"), and it suggests **roles you didn't ask for** whose postings fit your skills as well as the ones you did.

## Where the data comes from

- **Public job-board APIs only:** Greenhouse, Lever and Ashby, the systems most tech companies use for their careers pages, plus RemoteOK. No logins, and no scraping of LinkedIn, Indeed or Glassdoor (their terms forbid it).
- **208 companies**, verified to have a live public board, ship in `orgs.json`: companies linked from the last three HN "Who is hiring?" threads, plus well-known tech employers.
  - `whatshiring discover` finds more from HN.
  - Add yours by PR.
- **Polite:** one request per second per host, `Retry-After` honoured, and a User-Agent that says who's asking.

## Four safeguards, each found necessary on real data

1. **A company's blurb isn't a job requirement.** Sentences that appear in at least half of one company's postings (its About, benefits and EEO text) are skipped when extracting skills.
2. **One employer can't be the market.** At most 20 postings per company per window, chosen deterministically.
3. **Testing 100+ skills at once finds "trends" by chance.** A skill only counts as rising or falling if a Fisher exact test survives a Holm correction across all of them. Every change is shown with a 95% interval.
4. **Boards forget filled jobs.** Postings vanish when they're filled, so "posted 5–8 weeks ago" is undercounted. Until whatshiring has 4 weeks of its own snapshots, it compares only *shares* of new postings and makes no company-growth claims. After that, it compares postings *open now* with postings *open 4 weeks ago*, which has no such bias.
   - The weekly job carries history between runs by reading last week's snapshot from the published site. No data is committed to the repo.

## The weekly report

`.github/workflows/weekly.yml` runs every Monday:
1. loads last week's snapshot
2. fetches every board
3. publishes `index.html`, `data/snapshot.json` (postings without descriptions) and `data/trends.json` to GitHub Pages

Fork the repo, enable Pages (source: GitHub Actions), and you have your own.

## Prior art

- [hnhiring](https://hnhiring.com) and [hntrends](https://www.hntrends.com) track HN "Who is hiring?" posts, about 400 a month. whatshiring reads the companies' actual job boards (thousands of postings) and uses HN only to *discover* companies.
- [JobSpy](https://github.com/speedyapply/JobSpy) scrapes LinkedIn, Indeed and Glassdoor. It's broader, but against those sites' terms and risky for your accounts.
- Paid labour-market data (Lightcast and others) covers far more employers. whatshiring is free, reproducible, and says exactly which companies it covers.

## Honest limits

- **Coverage is 208 companies,** skewed toward tech companies that use Greenhouse, Lever or Ashby and toward the HN crowd. It is not "the job market", and the report says so.
- **Skill matching is a curated list** (`skills.json`, 126 skills with aliases and context patterns for words like Go, C and R). It can miss a skill phrased oddly and can't tell "required" from "nice to have".
- **Role families and seniority come from titles,** so "Member of Technical Staff" lands in a generic bucket.
- **The first four weekly reports use the posted-date comparison,** with its caveat printed at the top.

## License

MIT (code). Job postings belong to the companies that published them. The snapshot keeps titles, links and derived fields, not full descriptions.
