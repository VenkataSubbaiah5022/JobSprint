#!/usr/bin/env python3
"""Autonomous Naukri job application agent."""

from __future__ import annotations

import argparse
import json
import random
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
from preflight import print_preflight_results, validate_startup
from profile_refresh import (
    dismiss_naukri_modals,
    refresh_profile_visibility,
    should_refresh_profile,
)
from questionnaire import (
    complete_application_questionnaire,
    is_application_complete,
    is_job_unavailable,
)
from search_state import roles_for_cycle

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


def prepare_locations(locations: list[str]) -> tuple[list[str], bool]:
    """Deduplicate cities (Bangalore/Bengaluru) and detect Remote preference."""
    cities: list[str] = []
    seen: set[str] = set()
    include_remote = False
    for loc in locations:
        low = loc.lower().strip()
        if low == "remote":
            include_remote = True
            continue
        if low in ("bangalore", "bengaluru"):
            key = "bangalore"
            label = "Bengaluru"
        else:
            key = low
            label = loc.strip()
        if key not in seen:
            seen.add(key)
            cities.append(label)
    return cities, include_remote


def locations_label(config: dict) -> str:
    cities, remote = prepare_locations(config.get("locations", []))
    parts = list(cities)
    if remote:
        parts.append("Remote")
    return ", ".join(parts) if parts else "India"


def build_role_search_url(role: str, config: dict) -> str:
    """One search URL per role with all locations combined (not one URL per city)."""
    role_slug = slugify_role(role)
    exp = config["filters"].get("candidate_experience_years", 1)
    max_days = config["filters"]["max_posting_days"]
    job_age = 1 if max_days <= 1 else (3 if max_days <= 3 else 7)
    cities, include_remote = prepare_locations(config.get("locations", []))

    params = [
        f"k={quote_plus(role)}",
        f"experience={exp}",
        f"jobAge={job_age}",
    ]
    if cities:
        params.append("location=" + ",".join(quote_plus(c) for c in cities))
    if include_remote:
        params.append("qWfhType=2")

    if len(cities) == 1:
        loc_slug = slugify_role(cities[0])
        base = f"https://www.naukri.com/{role_slug}-jobs-in-{loc_slug}"
    else:
        base = f"https://www.naukri.com/{role_slug}-jobs"

    return f"{base}?{'&'.join(params)}"


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


def set_location_filters(page: Page, config: dict) -> list[str]:
    """Select multiple cities + remote on Naukri SRP in one pass."""
    cities, include_remote = prepare_locations(config.get("locations", []))
    applied: list[str] = []

    if cities:
        opened = open_filter_section(page, "Location") or open_filter_section(page, "City")
        if opened:
            for city in cities:
                clicked = False
                for variant in (city, "Bangalore" if city == "Bengaluru" else city):
                    if click_filter_option(page, variant):
                        applied.append(variant)
                        clicked = True
                        break
                if not clicked:
                    applied.append(city)

        if len(cities) > 1:
            loc_box = page.locator(
                "input[placeholder*='location' i], .nI-gNb-sb__loc input, #qsbLocation"
            )
            if loc_box.count():
                try:
                    loc_box.first.click(force=True)
                    loc_box.first.fill(", ".join(cities))
                    page.wait_for_timeout(1000)
                    for city in cities:
                        suggestion = page.locator(
                            f"span:has-text('{city}'), li:has-text('{city}'), div:has-text('{city}')"
                        )
                        if suggestion.count():
                            suggestion.first.click(force=True)
                            page.wait_for_timeout(400)
                except Exception:
                    pass

    if include_remote:
        for section in ("Work mode", "Work Mode", "WFH"):
            if open_filter_section(page, section):
                break
        for label in ("Remote", "Work from home", "WFH"):
            if click_filter_option(page, label):
                applied.append("Remote")
                break

    return applied


def apply_srp_filters(page: Page, config: dict) -> list[str]:
    freshness_labels = config["filters"].get(
        "freshness_options",
        ["Last 1 day", "Last 3 days", "Last 7 days"],
    )

    set_experience_filter(page, config)
    applied_locations = set_location_filters(page, config)

    if open_filter_section(page, "Freshness"):
        for label in freshness_labels:
            if click_filter_option(page, label):
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

    return applied_locations


def scroll_job_listings(page: Page, scrolls: int = 3) -> None:
    for _ in range(scrolls):
        try:
            page.keyboard.press("End")
            page.wait_for_timeout(1200)
        except Exception:
            break


def run_filtered_search(page: Page, role: str, config: dict) -> list[str]:
    search_url = build_role_search_url(role, config)
    page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    dismiss_naukri_modals(page)
    applied_locations = apply_srp_filters(page, config)
    scroll_job_listings(page, scrolls=config.get("settings", {}).get("listing_scrolls", 3))
    return applied_locations


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


def job_id_from_href(href: str) -> str:
    for pattern in (
        r"-(\d{8,})(?:\?|$)",
        r"job-listings-(\d+)",
        r"job-details/(\d+)",
        r"/(\d{9,})",
    ):
        match = re.search(pattern, href)
        if match:
            return match.group(1)
    return href


def extract_job_card(card, *, srp_urls_only: bool = True) -> dict | None:
    try:
        title_el = card.locator("a.title, .title, h2 a, a[href*='job']").first
        title = title_el.inner_text(timeout=2000).strip()
        href = title_el.get_attribute("href") or ""
        if not title:
            return None
        if srp_urls_only and "job-listings" not in href:
            return None
        if not href:
            return None

        company = safe_card_text(card, ".comp-name, .companyInfo .comp-name, .company")
        location = safe_card_text(card, ".locWdth, .locationsContainer, .location, .loc")
        exp = safe_card_text(card, ".expwdth, .experience, .exp")
        salary = safe_card_text(card, ".sal, .salary")
        posted = safe_card_text(card, ".job-post-day, .type, .tuple-posted-date")
        snippet = safe_card_text(card, ".job-desc, .row3, .tags-gt")
        skills = safe_card_text(card, ".tags-gt, .tag, [class*='tag']")
        url = href if href.startswith("http") else f"https://www.naukri.com{href}"
        return {
            "title": title,
            "company": company,
            "location": location,
            "experience": exp,
            "salary": salary,
            "posted": posted,
            "url": url,
            "job_id": job_id_from_href(href),
            "description": "",
            "listing_snippet": f"{snippet} {skills}",
            "skills": skills,
        }
    except Exception:
        return None


def extract_jobs_from_listing(page: Page, config: dict | None = None) -> list[dict]:
    jobs: list[dict] = []
    limit = 30
    if config:
        limit = config.get("settings", {}).get("jobs_per_search", 40)
    cards = page.locator(".srp-jobtuple-wrapper, .cust-job-tuple, article.jobTuple")
    for i in range(min(cards.count(), limit)):
        job = extract_job_card(cards.nth(i), srp_urls_only=True)
        if job:
            jobs.append(job)
    return jobs


def extract_jobs_from_link_fallback(page: Page, limit: int = 40) -> list[dict]:
    """Fallback when card wrappers are missing (common on recommended-jobs page)."""
    jobs: list[dict] = []
    seen: set[str] = set()
    links = page.locator("a[href*='job-listings'], a[href*='job-details']")
    for i in range(min(links.count(), limit * 2)):
        link = links.nth(i)
        try:
            href = link.get_attribute("href") or ""
            if not href or href in seen:
                continue
            title = link.inner_text(timeout=1500).strip()
            if len(title) < 4 or title.lower() in ("view all", "apply", "save"):
                continue
            seen.add(href)
            url = href if href.startswith("http") else f"https://www.naukri.com{href}"
            jobs.append({
                "title": title,
                "company": "",
                "location": "",
                "experience": "",
                "salary": "",
                "posted": "",
                "url": url,
                "job_id": job_id_from_href(href),
                "description": "",
                "listing_snippet": "",
                "skills": "",
            })
            if len(jobs) >= limit:
                break
        except Exception:
            continue
    return jobs


def extract_jobs_from_recommended(page: Page, config: dict | None = None) -> list[dict]:
    """Parse Naukri's recommended-jobs feed (profile-matched listings)."""
    limit = 40
    if config:
        limit = config.get("settings", {}).get("jobs_per_search", 40)

    scroll_job_listings(page, scrolls=4)
    jobs: list[dict] = []
    cards = page.locator(
        "article.jobTuple, .jobTuple, .srp-jobtuple-wrapper, .cust-job-tuple, "
        "[class*='reco'] article, [class*='jobTuple']"
    )
    for i in range(min(cards.count(), limit)):
        try:
            cards.nth(i).scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        job = extract_job_card(cards.nth(i), srp_urls_only=False)
        if job:
            jobs.append(job)

    if not jobs:
        jobs = extract_jobs_from_link_fallback(page, limit=limit)
    return jobs


def collect_recommended_jobs(context: BrowserContext, config: dict) -> list[dict]:
    if not config["settings"].get("use_recommended_feed", True):
        return []

    page = context.new_page()
    try:
        page.goto(
            "https://www.naukri.com/mnjuser/recommendedjobs",
            wait_until="domcontentloaded",
            timeout=45000,
        )
        page.wait_for_timeout(3000)
        dismiss_naukri_modals(page)
        return extract_jobs_from_recommended(page, config)
    except Exception as exc:
        print(f"Recommended feed failed: {exc}", flush=True)
        return []
    finally:
        page.close()


def merge_jobs(queue: dict[str, dict], jobs: list[dict]) -> None:
    for job in jobs:
        key = job.get("job_id") or job.get("url", "")
        if key and key not in queue:
            queue[key] = job


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
    candidate = config["candidate"]
    mappings = {
        r"experience|years of experience": answers["experience"],
        r"current ctc|current salary|present ctc": answers["current_ctc"],
        r"expected ctc|expected salary|desired ctc": answers["expected_ctc"],
        r"notice period|serving notice": answers["notice_period"],
        r"relocation|relocate|willing to relocate": answers["relocation"],
        r"phone|mobile|contact|whatsapp": answers.get("phone", ""),
        r"email": candidate.get("email", ""),
        r"linkedin": candidate.get("linkedin", ""),
        r"github": candidate.get("github", ""),
        r"portfolio|website": candidate.get("portfolio", ""),
        r"current location|current city": answers.get("current_location", ""),
        r"full.?name|your name": candidate.get("name", ""),
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
                if answer and re.search(pattern, context, re.I):
                    tag = field.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        field.select_option(label=answer)
                    else:
                        field.fill(str(answer))
                    break
        except Exception:
            continue


def locate_company_site_button(page: Page):
    selectors = [
        "button:has-text('Apply on company site')",
        "a:has-text('Apply on company site')",
        ".company-site-button",
        "[class*='company-site-button']",
        "[class*='companySite']",
    ]
    for sel in selectors:
        loc = page.locator(sel).filter(has_not_text="Applied")
        for i in range(min(loc.count(), 3)):
            btn = loc.nth(i)
            try:
                if btn.is_visible():
                    return btn
            except Exception:
                continue
    return None


def locate_naukri_apply_button(page: Page):
    """Easy Apply / in-Naukri apply only — not company-site redirects."""
    apply_selectors = [
        "button:has-text('Easy Apply')",
        "button:has-text('Apply')",
        "a:has-text('Apply')",
        ".apply-button",
        "#apply-button",
        "[class*='apply-button']",
    ]
    for sel in apply_selectors:
        loc = page.locator(sel).filter(has_not_text="Applied")
        for i in range(min(loc.count(), 5)):
            btn = loc.nth(i)
            try:
                if not btn.is_visible():
                    continue
                text = btn.inner_text(timeout=1000).lower()
                if "company site" in text:
                    continue
                return btn
            except Exception:
                continue
    return None


def capture_company_site_url(page: Page, context: BrowserContext, btn) -> str:
    """Get external apply URL without filling any company-site form."""
    try:
        url = btn.evaluate(
            """(el) => {
            const link = el.closest('a[href]');
            if (link) {
                const href = link.href || '';
                if (href && !/naukri\\.com/i.test(href)) return href;
            }
            const direct = el.getAttribute('href')
                || el.dataset.url
                || el.dataset.href
                || el.dataset.redirect
                || '';
            if (direct && !/naukri\\.com/i.test(direct)) return direct;
            return '';
        }"""
        )
        if url:
            return url.strip()
    except Exception:
        pass

    popup = None
    try:
        with context.expect_page(timeout=12000) as new_page_info:
            btn.click(force=True, timeout=5000)
        popup = new_page_info.value
        popup.wait_for_load_state("domcontentloaded", timeout=15000)
        popup.wait_for_timeout(1000)
        captured = (popup.url or "").strip()
        return captured
    except Exception:
        return ""
    finally:
        if popup is not None and not popup.is_closed():
            try:
                popup.close()
            except Exception:
                pass


def apply_to_job(page: Page, context: BrowserContext, config: dict, resume_path: Path) -> tuple[str, str]:
    if page.locator("text=Applied").count() or page.locator("#already-applied").count():
        return "already_applied", ""

    if is_job_unavailable(page):
        return "job_unavailable", ""

    page.wait_for_timeout(1500)

    company_site_btn = locate_company_site_button(page)
    if company_site_btn:
        company_site_btn.scroll_into_view_if_needed()
        company_url = capture_company_site_url(page, context, company_site_btn)
        return "saved_for_manual", company_url

    apply_btn = locate_naukri_apply_button(page)
    popup_page = page

    if apply_btn:
        apply_btn.scroll_into_view_if_needed()
        try:
            apply_btn.click(force=True, timeout=5000)
            page.wait_for_timeout(2000)
        except PlaywrightTimeout:
            apply_btn.click(force=True, timeout=5000)
            page.wait_for_timeout(2000)
    else:
        clicked_info = page.evaluate(
            """() => {
            const btn = [...document.querySelectorAll('button, a')]
              .find(el => /apply/i.test(el.innerText) && !/applied/i.test(el.innerText) && el.offsetParent);
            if (!btn) return { clicked: false, companySite: false };
            const companySite = /company\\s*site/i.test(btn.innerText || '');
            if (companySite) return { clicked: false, companySite: true };
            btn.scrollIntoView({block: 'center'});
            btn.click();
            return { clicked: true, companySite: false };
        }"""
        )
        if clicked_info.get("companySite"):
            company_site_btn = locate_company_site_button(page)
            if company_site_btn:
                company_url = capture_company_site_url(page, context, company_site_btn)
                return "saved_for_manual", company_url
            return "saved_for_manual", ""
        if not clicked_info.get("clicked"):
            return "no_apply_button", ""
        page.wait_for_timeout(2000)

    target = popup_page
    if resume_path.exists():
        file_input = target.locator("input[type='file']")
        if file_input.count():
            file_input.first.set_input_files(str(resume_path))

    for form_page in {page, target}:
        fill_screening_questions(form_page, config)
        complete_application_questionnaire(form_page, config)

    for submit_sel in [
        "button:has-text('Save and Apply')",
        "button:has-text('Submit')",
        "button:has-text('Apply')",
        "button:has-text('Send')",
        "input[type='submit']",
    ]:
        submit = target.locator(submit_sel).filter(has_not_text="Applied")
        if submit.count():
            try:
                submit.first.click(force=True, timeout=3000)
                target.wait_for_timeout(2500)
                complete_application_questionnaire(target, config)
            except Exception:
                pass
            break

    if is_application_complete(target) or is_application_complete(page):
        if popup_page is not page:
            popup_page.close()
        return "applied", ""

    if popup_page is not page:
        popup_page.close()
        return "external_redirect", ""

    return "attempted", ""


def process_job(
    context: BrowserContext,
    job: dict,
    config: dict,
    logger: ApplicationLogger,
    resume_path: Path,
    dry_run: bool = False,
) -> str | None:
    if logger.is_duplicate(job["job_id"], job["company"], job["title"]):
        print(f"[skip:duplicate] {job.get('title')} @ {job.get('company')}", flush=True)
        return None

    early_skip = should_skip_job_early(job, config)
    if early_skip:
        print(f"[skip:{early_skip}] {job.get('title')} @ {job.get('company')}", flush=True)
        return None

    preview_score = calculate_match_score(job, config, preview=True)
    if not should_apply_to_job(job, config, preview=True):
        logger.log_application(
            company=job.get("company", ""),
            role=job.get("title", ""),
            link=job.get("url", ""),
            status=f"skipped_low_match_{preview_score}",
            match_score=preview_score,
            job_id=job.get("job_id", ""),
            block_retry=False,
        )
        print(f"[skip:low_match_{preview_score}%] {job.get('title')} @ {job.get('company')}", flush=True)
        return None

    if dry_run:
        logger.log_application(
            company=job.get("company", ""),
            role=job.get("title", ""),
            link=job.get("url", ""),
            status=f"dry_run_{preview_score}",
            match_score=preview_score,
            job_id=job.get("job_id", ""),
            block_retry=False,
        )
        print(f"[dry_run] {preview_score}% | {job.get('title')} @ {job.get('company')}", flush=True)
        return "dry_run"

    print(
        f"[visit] {preview_score}% | {job.get('title')} @ {job.get('company')}\n"
        f"        {job.get('url')}",
        flush=True,
    )

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
                print(f"[click-apply] {match_score}% | {job.get('title')} @ {job.get('company')}", flush=True)
                status, company_site_url = apply_to_job(job_page, context, config, resume_path)
                if status == "saved_for_manual":
                    logger.log_manual_apply(
                        company=job.get("company", ""),
                        role=job.get("title", ""),
                        naukri_link=job.get("url", ""),
                        company_site_url=company_site_url,
                        match_score=match_score,
                        job_id=job.get("job_id", ""),
                    )
                    print(
                        f"[manual] company-site apply saved → data/manual_apply_queue.csv\n"
                        f"         site: {company_site_url or '(open from Naukri link)'}",
                        flush=True,
                    )
                    break
                if status in ("applied", "already_applied", "external_redirect", "attempted"):
                    break
            except PlaywrightTimeout:
                status = f"timeout_attempt_{attempt + 1}"
                job_page.wait_for_timeout(2000)
            except Exception as exc:
                status = f"error_attempt_{attempt + 1}: {exc}"
                job_page.wait_for_timeout(2000)
    finally:
        if status != "saved_for_manual":
            block = status in ("applied", "already_applied", "external_redirect")
            logger.log_application(
                company=job.get("company", ""),
                role=job.get("title", ""),
                link=job.get("url", ""),
                status=status,
                match_score=match_score,
                job_id=job.get("job_id", ""),
                block_retry=block,
            )
            print(f"[{status}] {match_score}% | {job.get('title')} @ {job.get('company')}", flush=True)
        job_page.close()
    return status


def apply_jobs_immediately(
    context: BrowserContext,
    jobs: list[dict],
    processed: set[str],
    config: dict,
    logger: ApplicationLogger,
    resume_path: Path,
    stats: dict[str, int],
    dry_run: bool,
    label: str,
) -> int:
    """Apply every qualifying job from this search result before the next search."""
    settings = config["settings"]
    max_applies = settings.get("max_applies_per_cycle", 120)
    delay_range = settings.get("apply_delay_seconds", [3, 8])
    delay_min = delay_range[0] if len(delay_range) > 0 else 3
    delay_max = delay_range[1] if len(delay_range) > 1 else 8

    if not dry_run and stats["applied"] >= max_applies:
        return 0

    pending = []
    for job in jobs:
        key = job.get("job_id") or job.get("url", "")
        if key and key not in processed:
            pending.append(job)

    if not pending:
        return 0

    ranked = sorted(
        pending,
        key=lambda j: calculate_match_score(j, config, preview=True),
        reverse=True,
    )

    print(
        f"\n>>> APPLY NOW ({label}): {len(ranked)} jobs "
        f"| applied so far: {stats['applied']}/{max_applies} <<<",
        flush=True,
    )

    applied_count = 0
    for job in ranked:
        key = job.get("job_id") or job.get("url", "")
        processed.add(key)

        if not dry_run and stats["applied"] >= max_applies:
            print(f"[apply] Per-cycle cap reached ({max_applies})", flush=True)
            break

        result = process_job(context, job, config, logger, resume_path, dry_run=dry_run)
        if result == "applied":
            stats["applied"] += 1
            applied_count += 1
            time.sleep(random.uniform(delay_min, delay_max))
        elif result == "dry_run":
            stats["dry_run"] += 1

    return applied_count


def run_search_cycle(
    context: BrowserContext,
    config: dict,
    logger: ApplicationLogger,
    resume_path: Path,
    dry_run: bool = False,
) -> dict[str, int]:
    queue: dict[str, dict] = {}
    processed: set[str] = set()
    settings = config["settings"]
    max_applies = settings.get("max_applies_per_cycle", 120)

    stats: dict[str, int] = {
        "found": 0,
        "applied": 0,
        "dry_run": 0,
        "searches": 0,
    }

    print("\n========== SEARCH → APPLY (one role at a time) ==========", flush=True)

    loc_label = locations_label(config)
    roles = roles_for_cycle(config, DATA_DIR)
    total_searches = len(roles)

    for search_num, role in enumerate(roles, start=1):
        if not dry_run and stats["applied"] >= max_applies:
            print(f"\n[search] Apply cap reached — skipping remaining role searches", flush=True)
            break

        stats["searches"] += 1
        page = context.new_page()
        jobs: list[dict] = []
        try:
            print(
                f"\n[search {search_num}/{total_searches}] {role}\n"
                f"         locations: {loc_label}",
                flush=True,
            )
            applied_locs = run_filtered_search(page, role, config)
            freshness = config["filters"].get("freshness_options", ["Last 1 day"])[0]
            print(
                f"         filters: experience + {freshness} + locations "
                f"({', '.join(applied_locs) if applied_locs else loc_label})",
                flush=True,
            )
            jobs = extract_jobs_from_listing(page, config)
            before = len(queue)
            merge_jobs(queue, jobs)
            print(
                f"         listings: {len(jobs)} | new unique: {len(queue) - before} "
                f"| queue total: {len(queue)}",
                flush=True,
            )
        except Exception as exc:
            print(f"         search failed: {exc}", flush=True)
        finally:
            page.close()

        stats["found"] = len(queue)
        if jobs:
            applied_here = apply_jobs_immediately(
                context, jobs, processed, config, logger, resume_path, stats, dry_run,
                role,
            )
            print(f"         applied from this search: {applied_here}", flush=True)

    if config["settings"].get("use_recommended_feed", True):
        if not dry_run and stats["applied"] < max_applies:
            print("\n[bonus] recommended feed", flush=True)
            recommended = collect_recommended_jobs(context, config)
            merge_jobs(queue, recommended)
            stats["found"] = len(queue)
            print(f"         listings: {len(recommended)} | queue total: {len(queue)}", flush=True)
            if recommended:
                applied_here = apply_jobs_immediately(
                    context, recommended, processed, config, logger, resume_path, stats, dry_run,
                    "recommended feed",
                )
                print(f"         applied from recommended: {applied_here}", flush=True)

    remaining = [job for key, job in queue.items() if key not in processed]
    if remaining and (dry_run or stats["applied"] < max_applies):
        print(f"\n[final] {len(remaining)} jobs not yet attempted", flush=True)
        apply_jobs_immediately(
            context, remaining, processed, config, logger, resume_path, stats, dry_run,
            "remaining",
        )

    print(
        f"\n========== CYCLE DONE ==========\n"
        f"  Searches run : {stats['searches']}\n"
        f"  Unique jobs  : {stats['found']}\n"
        f"  Applied      : {stats['applied']}\n"
        f"  Dry-run hits : {stats['dry_run']}\n"
        f"================================",
        flush=True,
    )
    return stats


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


def release_browser(context: BrowserContext, config: dict) -> None:
    if config.get("settings", {}).get("browser_mode") == "cdp":
        return
    context.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous Naukri Job Application Agent")
    parser.add_argument("--login", action="store_true", help="Only perform login and exit")
    parser.add_argument("--once", action="store_true", help="Run one search cycle and exit")
    parser.add_argument(
        "--refresh-profile",
        action="store_true",
        help="Refresh Naukri profile/resume visibility and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and score jobs without submitting applications",
    )
    args = parser.parse_args()

    config = load_config()
    logger = ApplicationLogger(DATA_DIR)
    resume_path = BASE_DIR / config["settings"]["resume_path"]
    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    errors, warnings = validate_startup(config, BASE_DIR, resume_path)
    if not print_preflight_results(errors, warnings):
        return 1

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
        dismiss_naukri_modals(page)

        if is_logged_in(page):
            print("Using existing Chrome session — already logged in.")
        elif not ensure_login(context, page, args.login):
            release_browser(context, config)
            return 1

        if args.login:
            release_browser(context, config)
            return 0

        if args.refresh_profile:
            refresh_profile_visibility(page, config, resume_path, DATA_DIR)
            release_browser(context, config)
            return 0

        refresh_minutes = config["settings"]["refresh_interval_minutes"]
        profile_refresh_hours = config["settings"].get("profile_refresh_interval_hours", 24)
        daily_start = datetime.now()

        while True:
            if should_refresh_profile(DATA_DIR, profile_refresh_hours):
                print("Running scheduled profile visibility refresh...", flush=True)
                refresh_profile_visibility(page, config, resume_path, DATA_DIR)

            cycle_start = datetime.now()
            stats = run_search_cycle(
                context, config, logger, resume_path, dry_run=args.dry_run
            )
            elapsed = datetime.now() - cycle_start
            applied_today = logger.count_applied_today()
            if args.dry_run:
                print(
                    f"Cycle complete (dry run): {stats['dry_run']} would apply | "
                    f"{stats['found']} found | Elapsed: {elapsed}",
                    flush=True,
                )
            else:
                print(
                    f"Cycle complete: {stats['applied']} applied | "
                    f"{stats['found']} found | Applied today: {applied_today} | Elapsed: {elapsed}",
                    flush=True,
                )

            if args.once:
                break

            if applied_today >= config["settings"]["daily_target_max"]:
                print("Daily target reached. Sleeping until tomorrow...")
                time.sleep(max(3600, (daily_start + timedelta(days=1) - datetime.now()).total_seconds()))

            print(f"Sleeping {refresh_minutes} minutes before next refresh...")
            time.sleep(refresh_minutes * 60)

        release_browser(context, config)

    return 0


if __name__ == "__main__":
    sys.exit(main())
