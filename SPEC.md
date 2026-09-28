# whatshiring — SPEC

> What's actually hiring this week, what's rising, and what fits you.

Pulls public job boards every day, keeps snapshots, and answers three questions: **which roles and skills are rising or falling**, **which companies are ramping up**, and **which open roles fit your resume and the roles you gave it**. It also suggests roles you didn't list that fit you and are growing.

## The hook (README opens with this)
**A weekly public report**, built by GitHub Actions and published on GitHub Pages, from 10k+ live postings:
- "Mentions of Rust in backend postings: +18% in 4 weeks (from 11.2% to 13.2% of postings)"
- "40 companies doubled their open engineering roles this month"
- "New this week: 'AI evaluation engineer' as a title, 23 postings"

Every weekly issue is shareable on its own and links back to the repo. The same data powers the personal ranking, which never leaves your machine.

## Data sources (all public, no login, no scraping of logged-in sites)
- Greenhouse (`boards-api.greenhouse.io/v1/boards/{org}/jobs?content=true`), Lever (`api.lever.co/v0/postings/{org}`), Ashby (`api.ashbyhq.com/posting-api/job-board/{org}`). All verified working, Sept 2026.
- **Company discovery:** links to those boards found in HN "Who is hiring" threads (via the Algolia API), plus a seed list in the repo that users can extend by PR.
- RemoteOK's public API.
- Each source is polite: at most 1 request per second per host, conditional requests where supported, and a descriptive User-Agent.

## Must have (v1)
1. **`whatshiring fetch`:** snapshots every board into `~/.applyloop/jobs.sqlite` as the shared `job` record. It tracks `first_seen`/`last_seen`, so postings that open and close are measured, not guessed.
2. **Normalisation:**
   - role family from the title (a readable rules table: backend, frontend, ML/AI, data, infra/SRE, mobile, security, EM, …)
   - seniority (intern…principal)
   - remote/hybrid/onsite and country
   - compensation when the posting states it
3. **Skill extraction:** a curated skill taxonomy (`skills.json`, 126 tech skills with aliases, e.g. `k8s → Kubernetes`; context patterns for names that are also words: Go, C, R) matched on word boundaries. The local model is optional and only for skills missing from the taxonomy, which are then proposed for the file, never silently added.
4. **Trends (`whatshiring trends`)**, with four safeguards, each found necessary on real data:
   - **Two windows, stated in the report.** *History* mode (postings open now vs open 4 weeks ago, from our own snapshots) is used once 4 weeks of snapshots exist. Until then, *posted* mode compares postings published in the last 4 weeks with the 4 weeks before. Filled postings vanish from boards, so in posted mode only shares are compared and no company-growth claims are made.
   - **Company boilerplate is not a requirement.** Sentences in ≥ 50% of one company's postings (≥ 5 postings) are skipped when extracting skills: one company's About paragraph produced 450 of 475 "computer vision" mentions.
   - **At most 20 postings per company per window.** On the first real fetch (20,741 postings), uncapped "trends" such as computer vision rising came almost entirely from one company publishing 791 postings.
   - **Holm correction across all skills tested at once;** a skill is reported as rising or falling only if it survives.

   - share of postings that mention each skill, per role family, now vs 4 weeks ago, with a two-proportion interval
   - a change is reported only if its interval excludes 0 and there are at least 50 postings on each side
   - companies by net new postings
   - titles never seen before
5. **Personal fit (`whatshiring match --resume resume.json --roles "backend, platform"`):**
   - score = a transparent weighted sum: skill overlap (JD skills ∩ resume skills, weighted by how often the JD mentions them), role-family match, seniority distance, location/remote constraints, and freshness
   - every score is shown with its parts; no black box
   - **adjacent roles:** role families you didn't list whose skill profile overlaps your resume and whose posting count is rising
6. **Weekly report:** `whatshiring report --html` produces a static page with inline SVG charts. The repo ships a GitHub Actions workflow that fetches, builds and publishes to Pages every Monday.
7. **Stdlib only.** Ships as a package so `skills.json` and the verified `orgs.json` (208 boards) come with it.

## Won't do (v1)
- LinkedIn, Indeed or Glassdoor scraping (terms of service, and logged-in data).
- Salary predictions.
- Claiming the snapshot covers "the whole market": the report states which sources and how many companies it covers.

## Expectations → test cases
Fixtures: recorded API responses for each source (small JSON files) and a fake HTTP server.

| ID | Given | Then |
|---|---|---|
| E1 | Greenhouse, Lever and Ashby fixtures | Each maps to the shared `job` record; the HTML description becomes clean text |
| E2 | An HN thread fixture with board links | Extracts the org slugs, deduplicated |
| E3 | A posting in snapshot 1 and missing in snapshot 2 | `last_seen` is set; counted as closed |
| E4 | "k8s", "Kubernetes (EKS)" | Both map to Kubernetes; "Kubernetesque" doesn't |
| E5 | Newcombe (1998) example 56/70 vs 48/80 | The difference interval matches the published 0.0524 to 0.3339 |
| E6 | 30 postings in a group | No trend verdict ("not enough postings") |
| E7 | A resume with Go and Postgres, roles "backend" | Ranks a Go/Postgres backend role above a React frontend role; the score breakdown is printed |
| E8 | A resume that fits platform/SRE postings better than the listed role | "Adjacent role" suggestion, with the overlap and the trend |
| E9 | The fake server returns 429 with `Retry-After: 2` | Waits and retries; never more than 1 request/s per host |
| E10 | `report --html` | Self-contained, no external requests |

## Done when
E1–E10 pass, and the first weekly report is live on GitHub Pages with real numbers from real postings.
