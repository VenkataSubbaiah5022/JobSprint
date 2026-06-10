# JobSprint

Autonomous Naukri job application agent. It searches fresh listings on [Naukri.com](https://www.naukri.com), scores them against your profile, and applies automatically using your existing Chrome session.

## Features

- Mines Naukri's recommended-jobs feed each cycle (profile-matched listings)
- Searches 60+ target roles across Hyderabad, Bangalore, Bengaluru, and Remote
- Applies experience (1 year) and freshness filters (Today / Last 1 day / Last 3 days)
- Profile match scoring (role, skills, location, experience, salary)
- Applies when match score ≥ 50% or title is a strong role match
- Skips Senior, Lead, QA, DevOps-only, and other excluded roles
- Handles Naukri chatbot Q&A (radio + text) with rule-based answers from your profile
- Optional AI fallback for unknown screening questions (Groq first, then Gemini)
- Fills standard form fields (CTC, notice period, relocation, phone, links)
- Prevents duplicate applications
- Logs every attempt to CSV
- Refreshes searches every 15 minutes until stopped
- Keeps Naukri profile and resume "updated today" for recruiter visibility (complements auto-apply)

## Prerequisites

- Windows 10/11
- Python 3.10+
- Google Chrome (logged into Naukri)
- Resume PDF at `resume.pdf` in the project root

## Installation

```powershell
cd JobSprint
pip install -r requirements.txt
playwright install chromium
```

Copy your resume:

```powershell
copy "C:\path\to\your\resume.pdf" resume.pdf
```

## Quick Start

### 1. Start Chrome with remote debugging

This reuses your logged-in Chrome profile:

```powershell
.\start_chrome.ps1
```

Log into Naukri in Chrome if you are not already logged in.

### 2. Run the agent

**Continuous mode** (refreshes every 15 minutes):

```powershell
python agent.py
```

**Single search cycle** (test run):

```powershell
python agent.py --once
```

**Login only**:

```powershell
python agent.py --login
```

**Profile visibility refresh** (bumps profile + re-uploads resume):

```powershell
python agent.py --refresh-profile
```

Stop anytime with `Ctrl+C`.

**Test without applying:**

```powershell
python agent.py --once --dry-run
```

## Configuration

Copy `config.example.json` to `config.json` and fill in your details:

```powershell
copy config.example.json config.json
```

Edit `config.json` to customize your profile and filters.

| Section | Purpose |
|---------|---------|
| `candidate` | Name, email, experience, CTC, portfolio links |
| `target_roles` | Job titles to search for |
| `skills` | Skills used for match scoring |
| `locations` | Preferred cities and Remote |
| `filters` | Experience range, salary, freshness, min match score |
| `exclude_keywords` | Roles to skip (Senior, Lead, QA, etc.) |
| `screening_answers` | Auto-filled application form answers |
| `settings` | Refresh interval, retries, resume path, Chrome CDP URL |

### Key filter settings

```json
"filters": {
  "candidate_experience_years": 1,
  "min_match_score": 50,
  "max_posting_days": 7,
  "strong_role_match_threshold": 80
}
```

## How matching works

Each job is scored out of 100:

| Factor | Weight |
|--------|--------|
| Role title | 30% |
| Skills | 35% |
| Location | 15% |
| Experience | 10% |
| Salary | 10% |

**Apply if:**

- Score ≥ `min_match_score` (default 50), or
- Job title strongly matches a target role (≥ 80%), unless the title contains an excluded keyword (e.g. Senior, Lead)

Exclude keywords are checked on the **job title** during listing preview, not on skill tags scraped from the card (avoids false 0% scores).

## Screening & chatbot Q&A

During apply, JobSprint handles two flows:

1. **Standard forms** — CTC, notice period, relocation, file upload
2. **Naukri chatbot** — multi-step radio and text questions with Save/Continue

Answers are resolved in order:

1. **Rules from `screening_answers` + `candidate`** — fast, free, deterministic (CTC, notice, links, yes/no)
2. **Groq AI** (optional) — if `groq_api_key` is set and rules do not match
3. **Gemini AI** (optional) — used when Groq is unavailable or rate-limited
4. **Default** — `screening_answers.default` (usually "Yes")

Set your real phone in `screening_answers.phone` — many employers ask for it.

## Profile visibility

Recruiters often filter candidates by recently updated profiles. JobSprint can keep your Naukri profile and resume marked as updated today:

- Re-saves basic profile details (e.g. phone) to bump your last-updated timestamp
- Re-uploads your resume with a fresh PDF metadata stamp
- Runs automatically every `profile_refresh_interval_hours` (default 24), or on demand with `--refresh-profile`

## Project structure

```
JobSprint/
├── agent.py           # Main agent loop
├── matcher.py         # Match scoring logic
├── questionnaire.py   # Chatbot Q&A (rules + optional AI)
├── profile_refresh.py # Profile/resume visibility for recruiters
├── logger.py          # Duplicate tracking and CSV logging
├── config.json        # Profile and filter configuration (local, gitignored)
├── config.example.json
├── start_chrome.ps1   # Launch Chrome with CDP on port 9222
├── requirements.txt
├── resume.pdf         # Your resume (add this)
└── data/
    ├── applications_log.csv   # Application history
    └── applied_jobs.json      # Duplicate prevention
```

## Application log

Check `data/applications_log.csv` for:

| Column | Description |
|--------|-------------|
| Date | When the job was processed |
| Company | Employer name |
| Role | Job title |
| Link | Naukri job URL |
| Status | `applied`, `skipped_low_match_X`, `external_redirect`, etc. |
| Match Score | Profile match percentage |

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Could not connect to Chrome` | Run `.\start_chrome.ps1` first |
| Login page keeps appearing | Complete Google login in Chrome, then rerun |
| All jobs show `skipped_low_match` | Lower `min_match_score` in `config.json` |
| No resume uploaded | Place `resume.pdf` in the JobSprint folder |
| Chrome closes other tabs | Expected — `start_chrome.ps1` restarts Chrome with debugging enabled |

## Disclaimer

Use responsibly and in line with Naukri.com's terms of service. Automated applications should only be sent to roles that genuinely match your profile. Review `config.json` and application logs regularly.
