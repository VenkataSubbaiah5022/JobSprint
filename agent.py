#!/usr/bin/env python3
"""Autonomous Naukri job application agent."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from logger import ApplicationLogger
from matcher import calculate_match_score, should_apply_to_job

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
BROWSER_DATA_DIR = BASE_DIR / "browser_data"


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def slugify_role(role: str) -> str:
    slug = role.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def build_search_url(role: str, location: str, config: dict) -> str:
    role_slug = slugify_role(role)
    exp = config["filters"].get("candidate_experience_years", 1)
    max_days = config["filters"]["max_posting_days"]
    job_age = 1 if max_days <= 1 else (3 if max_days <= 3 else 7)
    if location.lower() == "remote":
        return (
            f"https://www.naukri.com/{role_slug}-jobs"
            f"?k={quote_plus(role)}&experience={exp}&jobAge={job_age}&qWfhType=2"
        )
    loc_slug = slugify_role(location)
    return (
        f"https://www.naukri.com/{role_slug}-jobs-in-{loc_slug}"
        f"?experience={exp}&jobAge={job_age}"
    )


def safe_card_text(card, selectors: str, default: str = "") -> str:
    for sel in selectors.split(", "):
        loc = card.locator(sel.strip())
        if loc.count():
            try:
                return loc.first.inner_text(timeout=1500).strip()
            except Exception:
                continue
    return default


def parse_experience_range(text: str) -> tuple[int | None, int | None]:
    match = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*yrs?", (text or "").lower())
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"(\d+)\+?\s*yrs?", (text or "").lower())
    if match:
        val = int(match.group(1))
        return val, val
    url_match = re.search(r"(\d+)-to-(\d+)-years", (text or "").lower())
    if url_match:
        return int(url_match.group(1)), int(url_match.group(2))
    return None, None


def should_skip_job_early(job: dict, config: dict) -> str | None:
    title = job.get("title", "")
    title_l = title.lower()
    for kw in config["exclude_keywords"]:
        if re.search(rf"\b{re.escape(kw.lower())}\b", title_l):
            return f"excluded_title_{kw}"

    exp_text = f"{job.get('experience', '')} {job.get('url', '')}"
    min_exp, max_exp = parse_experience_range(exp_text)
    allowed_max = config["filters"]["experience_max"]
    if min_exp is not None and min_exp > allowed_max + 1:
        return f"experience_min_{min_exp}"

    posted_days = parse_posted_days(job.get("posted", ""))
    if posted_days > config["filters"]["max_posting_days"]:
        return f"stale_{posted_days}d"

    return None


def click_filter_option(page: Page, option_text: str) -> bool:
    patterns = [
        f"label:has-text('{option_text}')",
        f"span:has-text('{option_text}')",
        f"a:has-text('{option_text}')",
        f"div:has-text('{option_text}')",
    ]
    for pattern in patterns:
        opt = page.locator(pattern)
        for i in range(min(opt.count(), 5)):
            candidate = opt.nth(i)
            try:
                if candidate.is_visible():
                    candidate.click(force=True)
                    page.wait_for_timeout(1200)
                    return True
            except Exception:
                continue
    return False


def open_filter_section(page: Page, section_name: str) -> bool:
    locators = [
        page.get_by_text(section_name, exact=True),
        page.locator(f"[class*='filter']:has-text('{section_name}')"),
        page.locator(f"div:has-text('{section_name}')").filter(has_text=re.compile(rf"^{section_name}$")),
    ]
    for loc in locators:
        if loc.count():
            try:
                loc.first.click(force=True)
                page.wait_for_timeout(700)
                return True
            except Exception:
                continue
    return False


def set_experience_filter(page: Page, config: dict) -> None:
    exp_years = config["filters"].get("candidate_experience_years", 1)
    exp_max = config["filters"]["experience_max"]
    exp_options = [
        f"{exp_years} year",
        f"{exp_years} years",
        f"0-{exp_max} Yrs",
        "0-3 Yrs",
        "0-2 Yrs",
        "1-3 Yrs",
    ]

    exp_input = page.locator("#experienceDD, input[name='experienceDD'], input[placeholder*='experience' i]")
    if exp_input.count():
        try:
            exp_input.first.click(force=True)
            page.wait_for_timeout(500)
            for option in exp_options:
                if click_filter_option(page, option):
                    search_btn = page.locator("button:has-text('Search'), .qsbSubmit, .nI-gNb-sb__icon-wrapper")
                    if search_btn.count():
                        search_btn.first.click(force=True)
                        page.wait_for_timeout(2500)
                    return
        except Exception:
            pass

    if open_filter_section(page, "Experience"):
        for option in exp_options:
            if click_filter_option(page, option):
                return


def apply_srp_filters(page: Page, config: dict) -> None:
    freshness_labels = config["filters"].get(
        "freshness_options",
        ["Last 1 day", "Last 3 days", "Last 7 days"],
    )

    set_experience_filter(page, config)

    if open_filter_section(page, "Freshness"):
        for label in freshness_labels:
            if click_filter_option(page, label):
                print(f"Applied freshness filter: {label}", flush=True)
                break

    sort_by = page.locator("text=Sort by")
    if sort_by.count():
        try:
            sort_by.first.click(force=True)
            page.wait_for_timeout(500)
            for sort_opt in ["Date", "Freshness", "Newest"]:
                if click_filter_option(page, sort_opt):
                    break
        except Exception:
            pass


def run_filtered_search(page: Page, role: str, location: str, config: dict) -> None:
    search_url = build_search_url(role, location, config)
    page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)
    apply_srp_filters(page, config)


def is_logged_in(page: Page) -> bool:
    try:
        url = page.url
        if "nlogin/login" in url:
            return False
        if "/mnjuser/" in url:
            return True
        if page.locator("a[title='My Naukri Home']").count() > 0:
            return True
        if page.locator("text=Recommended Jobs").count() > 0:
            return True
        if page.locator(".nI-gNb-drawer__user-name, .user-name, #userName").count() > 0:
            return True
        return False
    except Exception:
        return False


def wait_for_manual_login(page: Page, timeout_seconds: int = 300) -> bool:
    print("Complete Google login in the browser window...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        if is_logged_in(page):
            print("Login detected.")
            return True
    return False


def click_google_login(page: Page) -> None:
    google_btn = page.locator("a.socialbtn.google")
    if google_btn.count():
        google_btn.first.scroll_into_view_if_needed()
        with page.expect_popup(timeout=15000) as popup_info:
            google_btn.first.click()
        popup = popup_info.value
        popup.wait_for_load_state("domcontentloaded")
        email = load_config()["candidate"]["email"]
        identifier = popup.locator('input[type="email"]')
        if identifier.count() and identifier.first.is_visible():
            identifier.first.fill(email)
            popup.locator("#identifierNext, button:has-text('Next')").first.click()
        popup.wait_for_timeout(5000)
        try:
            popup.wait_for_event("close", timeout=120000)
        except PlaywrightTimeout:
            pass
    page.wait_for_timeout(5000)


def ensure_login(context: BrowserContext, page: Page, login_only: bool) -> bool:
    page.goto("https://www.naukri.com/mnjuser/recommendedjobs", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    if is_logged_in(page):
        return True
    click_google_login(page)
    if not wait_for_manual_login(page):
        print("Login not completed within timeout.")
        return False
    if login_only:
        print("Login successful. Re-run without --login to start applying.")
    return True


def parse_posted_days(posted_text: str) -> int:
    text = (posted_text or "").lower()
    if "just now" in text or "few hours" in text or "today" in text:
        return 0
    if "24 hour" in text or "1 day" in text:
        return 1
    match = re.search(r"(\d+)\s*days?", text)
    if match:
        return int(match.group(1))
    if "week" in text:
        return 7
    return 999


def extract_jobs_from_listing(page: Page) -> list[dict]:
    jobs: list[dict] = []
    cards = page.locator(".srp-jobtuple-wrapper, .cust-job-tuple, article.jobTuple")
    count = cards.count()
    for i in range(min(count, 30)):
        card = cards.nth(i)
        try:
            title_el = card.locator("a.title, .title, h2 a").first
            title = title_el.inner_text(timeout=2000).strip()
            href = title_el.get_attribute("href") or ""
            if not title or "job-listings" not in href:
                continue
            company = safe_card_text(card, ".comp-name, .companyInfo .comp-name, .company")
            location = safe_card_text(card, ".locWdth, .locationsContainer, .location, .loc")
            exp = safe_card_text(card, ".expwdth, .experience, .exp")
            salary = safe_card_text(card, ".sal, .salary")
            posted = safe_card_text(card, ".job-post-day, .type, .tuple-posted-date")
            snippet = safe_card_text(card, ".job-desc, .row3, .tags-gt")
            skills = safe_card_text(card, ".tags-gt, .tag, [class*='tag']")
            job_id_match = re.search(r"-(\d{8,})(?:\?|$)", href) or re.search(r"job-listings-(\d+)", href)
            job_id = job_id_match.group(1) if job_id_match else href
            jobs.append({
                "title": title,
                "company": company,
                "location": location,
                "experience": exp,
                "salary": salary,
                "posted": posted,
                "url": href if href.startswith("http") else f"https://www.naukri.com{href}",
                "job_id": job_id,
                "description": "",
                "listing_snippet": f"{snippet} {skills}",
                "skills": skills,
            })
        except Exception:
            continue
    return jobs


def extract_job_details(page: Page) -> dict:
    details = {"description": "", "skills": "", "salary": "", "location": "", "experience": ""}
    jd_selectors = [
        ".styles_jd-container__nFVw8",
        ".styles_jdc__content__EZJMQ",
        ".styles_jd__parag__",
        ".jd-desc",
        "[class*='jd-container']",
    ]
    for sel in jd_selectors:
        loc = page.locator(sel)
        if loc.count():
            try:
                text = loc.first.inner_text(timeout=5000)
                if len(text) > 100:
                    details["description"] = text[:6000]
                    break
            except Exception:
                continue

    for label, key in [
        ("Key Skills", "skills"),
        ("Salary", "salary"),
        ("Location", "location"),
        ("Experience", "experience"),
    ]:
        try:
            label_el = page.locator(f"text={label}").first
            section = label_el.locator("xpath=ancestor::div[contains(@class,'styles')][1]")
            if section.count():
                details[key] = section.first.inner_text(timeout=2000)
        except Exception:
            pass
    return details


def fill_screening_questions(page: Page, config: dict) -> None:
    answers = config["screening_answers"]
    mappings = {
        r"experience|years of experience": answers["experience"],
        r"current ctc|current salary": answers["current_ctc"],
        r"expected ctc|expected salary|desired ctc": answers["expected_ctc"],
        r"notice period": answers["notice_period"],
        r"relocation|relocate|willing to relocate": answers["relocation"],
    }
    inputs = page.locator("input[type='text'], input[type='number'], textarea, select")
    for i in range(inputs.count()):
        field = inputs.nth(i)
        try:
            label_text = ""
            field_id = field.get_attribute("id") or ""
            if field_id:
                label = page.locator(f"label[for='{field_id}']")
                if label.count():
                    label_text = label.first.inner_text()
            placeholder = field.get_attribute("placeholder") or ""
            context = f"{label_text} {placeholder}".lower()
            for pattern, answer in mappings.items():
                if re.search(pattern, context, re.I):
                    tag = field.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        field.select_option(label=answer)
                    else:
                        field.fill(answer)
                    break
        except Exception:
            continue


def locate_apply_button(page: Page):
    apply_selectors = [
        "button:has-text('Easy Apply')",
        "button:has-text('Apply on company site')",
        "button:has-text('Apply')",
        "a:has-text('Apply')",
        ".apply-button",
        "#apply-button",
        ".company-site-button",
        "[class*='apply-button']",
        "[class*='company-site-button']",
    ]
    for sel in apply_selectors:
        loc = page.locator(sel).filter(has_not_text="Applied")
        for i in range(min(loc.count(), 3)):
            btn = loc.nth(i)
            try:
                if btn.is_visible():
                    return btn
            except Exception:
                continue
    return None


def apply_to_job(page: Page, context: BrowserContext, config: dict, resume_path: Path) -> str:
    if page.locator("text=Applied").count():
        return "already_applied"

    page.wait_for_timeout(1500)
    apply_btn = locate_apply_button(page)
    popup_page = page

    if apply_btn:
        apply_btn.scroll_into_view_if_needed()
        btn_text = apply_btn.inner_text(timeout=2000).lower()
        try:
            if "company site" in btn_text:
                with context.expect_page(timeout=15000) as new_page_info:
                    apply_btn.click(force=True, timeout=5000)
                popup_page = new_page_info.value
                popup_page.wait_for_load_state("domcontentloaded")
                popup_page.wait_for_timeout(2500)
            else:
                apply_btn.click(force=True, timeout=5000)
                page.wait_for_timeout(2000)
        except PlaywrightTimeout:
            apply_btn.click(force=True, timeout=5000)
            page.wait_for_timeout(2000)
    else:
        clicked = page.evaluate(
            """() => {
            const btn = [...document.querySelectorAll('button, a')]
              .find(el => /apply/i.test(el.innerText) && !/applied/i.test(el.innerText) && el.offsetParent);
            if (!btn) return false;
            btn.scrollIntoView({block: 'center'});
            btn.click();
            return true;
        }"""
        )
        if not clicked:
            return "no_apply_button"
        page.wait_for_timeout(2000)

    target = popup_page
    if resume_path.exists():
        file_input = target.locator("input[type='file']")
        if file_input.count():
            file_input.first.set_input_files(str(resume_path))

    fill_screening_questions(target, config)

    for submit_sel in [
        "button:has-text('Submit')",
        "button:has-text('Save and Apply')",
        "button:has-text('Apply')",
        "button:has-text('Send')",
        "input[type='submit']",
    ]:
        submit = target.locator(submit_sel)
        if submit.count():
            submit.first.click()
            target.wait_for_timeout(3000)
            break

    success_markers = [
        "successfully applied",
        "application submitted",
        "applied successfully",
        "thank you for applying",
    ]
    page_text = target.locator("body").inner_text(timeout=3000).lower()
    if any(marker in page_text for marker in success_markers) or target.locator("text=Applied").count():
        if popup_page is not page:
            popup_page.close()
        return "applied"

    if popup_page is not page:
        status = "external_redirect"
        popup_page.close()
        return status

    return "attempted"


def process_job(
    context: BrowserContext,
    job: dict,
    config: dict,
    logger: ApplicationLogger,
    resume_path: Path,
) -> None:
    if logger.is_duplicate(job["job_id"], job["company"], job["title"]):
        print(f"[duplicate] {job.get('title')} @ {job.get('company')}", flush=True)
        return

    early_skip = should_skip_job_early(job, config)
    if early_skip:
        print(f"[early_skip:{early_skip}] {job.get('title')} @ {job.get('company')}", flush=True)
        return

    preview_score = calculate_match_score(job, config, preview=True)
    if not should_apply_to_job(job, config, preview=True):
        logger.log_application(
            company=job.get("company", ""),
            role=job.get("title", ""),
            link=job.get("url", ""),
            status=f"skipped_low_match_{preview_score}",
            match_score=preview_score,
            job_id=job.get("job_id", ""),
        )
        print(f"[skipped_low_match_{preview_score}] {preview_score}% | {job.get('title')} @ {job.get('company')}", flush=True)
        return

    job_page = context.new_page()
    status = "failed"
    match_score = preview_score
    try:
        for attempt in range(config["settings"]["max_retries"]):
            try:
                job_page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
                job_page.wait_for_timeout(2000)
                details = extract_job_details(job_page)
                job.update(details)
                match_score = calculate_match_score(job, config, preview=False)
                if not should_apply_to_job(job, config, preview=False):
                    status = f"skipped_low_match_{match_score}"
                    break
                print(f"[applying] {match_score}% | {job.get('title')} @ {job.get('company')}", flush=True)
                status = apply_to_job(job_page, context, config, resume_path)
                if status in ("applied", "already_applied", "external_redirect", "attempted"):
                    break
            except PlaywrightTimeout:
                status = f"timeout_attempt_{attempt + 1}"
                job_page.wait_for_timeout(2000)
            except Exception as exc:
                status = f"error_attempt_{attempt + 1}: {exc}"
                job_page.wait_for_timeout(2000)
    finally:
        logger.log_application(
            company=job.get("company", ""),
            role=job.get("title", ""),
            link=job.get("url", ""),
            status=status,
            match_score=match_score,
            job_id=job.get("job_id", ""),
        )
        print(f"[{status}] {match_score}% | {job.get('title')} @ {job.get('company')}", flush=True)
        job_page.close()


def run_search_cycle(context: BrowserContext, config: dict, logger: ApplicationLogger, resume_path: Path) -> int:
    applied_count = 0
    locations = config["locations"]
    roles = config["target_roles"]

    for role in roles:
        for location in locations:
            page = context.new_page()
            try:
                run_filtered_search(page, role, location, config)
                jobs = extract_jobs_from_listing(page)
                print(f"Found {len(jobs)} jobs for '{role}' in '{location}' (filtered)", flush=True)
                for job in jobs:
                    before = len(logger.applied)
                    process_job(context, job, config, logger, resume_path)
                    if len(logger.applied) > before:
                        applied_count += 1
            except Exception as exc:
                print(f"Search failed for {role}/{location}: {exc}", flush=True)
            finally:
                page.close()
    return applied_count


def create_browser_context(p: Playwright, config: dict) -> BrowserContext:
    settings = config["settings"]
    mode = settings.get("browser_mode", "isolated")

    if mode == "cdp":
        cdp_url = settings.get("chrome_cdp_url", "http://localhost:9222")
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
            if browser.contexts:
                return browser.contexts[0]
            return browser.new_context(viewport={"width": 1400, "height": 900})
        except Exception as exc:
            print(f"Could not connect to Chrome at {cdp_url}: {exc}")
            print("Run: .\\start_chrome.ps1  (restarts Chrome with your logged-in session)")
            raise

    if mode == "chrome_profile":
        user_data = settings.get("chrome_user_data_dir", "")
        profile = settings.get("chrome_profile", "Default")
        return p.chromium.launch_persistent_context(
            user_data_dir=user_data,
            channel="chrome",
            headless=settings["headless"],
            viewport={"width": 1400, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                f"--profile-directory={profile}",
            ],
        )

    return p.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_DATA_DIR),
        headless=settings["headless"],
        viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous Naukri Job Application Agent")
    parser.add_argument("--login", action="store_true", help="Only perform login and exit")
    parser.add_argument("--once", action="store_true", help="Run one search cycle and exit")
    args = parser.parse_args()

    config = load_config()
    logger = ApplicationLogger(DATA_DIR)
    resume_path = BASE_DIR / config["settings"]["resume_path"]
    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Naukri Autonomous Job Application Agent")
    print(f"Candidate: {config['candidate']['name']}")
    print(f"Target: {config['settings']['daily_target_min']}-{config['settings']['daily_target_max']} apps/day")
    print("=" * 60)

    with sync_playwright() as p:
        context = create_browser_context(p, config)
        page = context.pages[0] if context.pages else context.new_page()

        page.goto("https://www.naukri.com/mnjuser/recommendedjobs", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if is_logged_in(page):
            print("Using existing Chrome session — already logged in.")
        elif not ensure_login(context, page, args.login):
            context.close()
            return 1

        if args.login:
            context.close()
            return 0

        refresh_minutes = config["settings"]["refresh_interval_minutes"]
        daily_start = datetime.now()

        while True:
            cycle_start = datetime.now()
            count = run_search_cycle(context, config, logger, resume_path)
            elapsed = datetime.now() - cycle_start
            total_today = len(logger.applied)
            print(f"Cycle complete: {count} new applications | Total tracked: {total_today} | Elapsed: {elapsed}")

            if args.once:
                break

            if total_today >= config["settings"]["daily_target_max"]:
                print("Daily target reached. Sleeping until tomorrow...")
                time.sleep(max(3600, (daily_start + timedelta(days=1) - datetime.now()).total_seconds()))

            print(f"Sleeping {refresh_minutes} minutes before next refresh...")
            time.sleep(refresh_minutes * 60)

        context.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
