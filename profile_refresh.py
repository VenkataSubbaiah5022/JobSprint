"""Keep Naukri profile and resume visibly fresh for recruiter discovery.

JobSprint applies to jobs outbound; this module improves inbound visibility
by keeping profile and resume timestamps current.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout
from pypdf import PdfReader, PdfWriter

PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
LAST_REFRESH_FILE = "last_profile_refresh.txt"

MODAL_DISMISS_SELECTORS = [
    "text=SKIP AND CONTINUE",
    "text=Skip and continue",
    "[class*='crossIcon']",
    "[alt='cross-icon']",
    "[class*='cross-icon']",
    "button:has-text('Maybe later')",
    "button:has-text('Not now')",
]


def dismiss_naukri_modals(page: Page) -> int:
    """Close promotional overlays that block search and apply flows."""
    dismissed = 0
    for sel in MODAL_DISMISS_SELECTORS:
        loc = page.locator(sel)
        for i in range(min(loc.count(), 3)):
            btn = loc.nth(i)
            try:
                if btn.is_visible():
                    btn.click(force=True, timeout=1500)
                    page.wait_for_timeout(400)
                    dismissed += 1
            except Exception:
                continue
    return dismissed


def _today_patterns() -> list[str]:
    today = datetime.today()
    return [
        today.strftime("%b %d, %Y"),
        f"{today.strftime('%b')} {today.day}, {today.strftime('%Y')}",
        today.strftime("%d %b %Y"),
        "today",
    ]


def _text_has_today(text: str) -> bool:
    lowered = (text or "").lower()
    if "today" in lowered:
        return True
    for pattern in _today_patterns():
        if pattern.lower() in lowered:
            return True
    return False


def touch_profile_fields(page: Page, phone: str) -> bool:
    """Re-save basic details so Naukri bumps the profile 'last updated' timestamp."""
    page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    dismiss_naukri_modals(page)

    view_profile = page.locator("a[class*='view-profile'], [class*='view-profile'] a").first
    if view_profile.count():
        try:
            view_profile.click(force=True, timeout=3000)
            page.wait_for_timeout(1500)
        except Exception:
            pass

    dismiss_naukri_modals(page)

    edit_btn = page.locator("[class*='icon edit'], button:has-text('Edit')").first
    if edit_btn.count():
        try:
            edit_btn.click(force=True, timeout=3000)
            page.wait_for_timeout(1000)
        except Exception:
            pass

    mobile = page.locator("input[name='mobile'], #mob_number, input[id*='mob']").first
    if mobile.count() and phone:
        try:
            mobile.fill(phone)
            page.wait_for_timeout(500)
        except Exception:
            pass

    save_selectors = [
        "button[type='submit'][value='Save Changes']",
        "#saveBasicDetailsBtn",
        "button:has-text('Save')",
    ]
    for sel in save_selectors:
        btn = page.locator(sel)
        if btn.count():
            try:
                btn.first.click(force=True, timeout=3000)
                page.wait_for_timeout(2500)
                break
            except Exception:
                continue

    body = page.locator("body").inner_text(timeout=3000)
    return _text_has_today(body)


def prepare_resume_variant(source: Path, dest_dir: Path) -> Path:
    """Write a resume copy with updated PDF metadata so uploads register as new."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"resume_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

    reader = PdfReader(str(source))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata(
        {
            "/Producer": "JobSprint",
            "/ModDate": datetime.now().strftime("D:%Y%m%d%H%M%S"),
        }
    )
    with open(dest, "wb") as out:
        writer.write(out)
    return dest


def upload_resume(page: Page, resume_path: Path) -> bool:
    """Re-upload resume and verify the on-page 'updated on' stamp reflects today."""
    page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    dismiss_naukri_modals(page)

    upload_selectors = [
        "input[type='file'][value='Update resume']",
        "#attachCV",
        "#lazyAttachCV",
        "input[type='file']",
    ]
    uploaded = False
    for sel in upload_selectors:
        loc = page.locator(sel)
        if loc.count():
            try:
                loc.first.set_input_files(str(resume_path))
                page.wait_for_timeout(2000)
                uploaded = True
                break
            except Exception:
                continue

    if not uploaded:
        return False

    save = page.locator("button[type='button']:has-text('Save'), button:has-text('Save')")
    if save.count():
        try:
            save.first.click(force=True, timeout=3000)
        except Exception:
            pass

    try:
        page.wait_for_selector("[class*='updateOn'], [class*='update-on']", timeout=20000)
    except PlaywrightTimeout:
        pass

    stamp = page.locator("[class*='updateOn'], [class*='update-on']").first
    if stamp.count():
        text = stamp.inner_text(timeout=3000)
        return _text_has_today(text)

    body = page.locator("body").inner_text(timeout=3000)
    return _text_has_today(body)


def refresh_profile_visibility(
    page: Page,
    config: dict,
    resume_path: Path,
    data_dir: Path,
) -> dict:
    """Run profile touch + optional resume re-upload. Returns status details."""
    settings = config.get("settings", {})
    if not settings.get("profile_refresh_enabled", True):
        return {"skipped": True, "reason": "disabled"}

    phone = config.get("screening_answers", {}).get("phone", "")
    touch_resume = settings.get("profile_refresh_touch_resume", True)
    variant_dir = data_dir / "resume_variants"

    result = {"profile_touched": False, "resume_uploaded": False, "errors": []}

    try:
        result["profile_touched"] = touch_profile_fields(page, phone)
        print(
            f"[profile] Basic details refresh: {'ok' if result['profile_touched'] else 'unverified'}",
            flush=True,
        )
    except Exception as exc:
        result["errors"].append(f"profile_touch: {exc}")
        print(f"[profile] Basic details refresh failed: {exc}", flush=True)

    if touch_resume and resume_path.exists():
        try:
            upload_path = prepare_resume_variant(resume_path, variant_dir)
            result["resume_uploaded"] = upload_resume(page, upload_path)
            print(
                f"[profile] Resume re-upload: {'ok' if result['resume_uploaded'] else 'unverified'}",
                flush=True,
            )
        except Exception as exc:
            result["errors"].append(f"resume_upload: {exc}")
            print(f"[profile] Resume re-upload failed: {exc}", flush=True)

    stamp = datetime.now().isoformat()
    (data_dir / LAST_REFRESH_FILE).write_text(stamp, encoding="utf-8")
    result["timestamp"] = stamp
    return result


def should_refresh_profile(data_dir: Path, interval_hours: float) -> bool:
    stamp_file = data_dir / LAST_REFRESH_FILE
    if not stamp_file.exists():
        return True
    try:
        last = datetime.fromisoformat(stamp_file.read_text(encoding="utf-8").strip())
        elapsed_hours = (datetime.now() - last).total_seconds() / 3600
        return elapsed_hours >= interval_hours
    except Exception:
        return True
