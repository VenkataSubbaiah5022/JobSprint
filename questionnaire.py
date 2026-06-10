"""Naukri application questionnaire handler — rule-based answers with optional AI fallback."""

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

SUCCESS_MARKERS = (
    "successfully applied",
    "you have successfully applied",
    "application submitted",
    "applied successfully",
    "thank you for applying",
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def is_application_complete(page: Page) -> bool:
    try:
        if page.locator("#already-applied, text=Applied").count():
            return True
        body = page.locator("body").inner_text(timeout=2000).lower()
        return any(marker in body for marker in SUCCESS_MARKERS)
    except Exception:
        return False


def is_job_unavailable(page: Page) -> bool:
    try:
        if page.locator("#already-applied").count():
            return True
        alerts = page.locator("[class*='alert-message'], [class*='alert-message-text']")
        for i in range(min(alerts.count(), 3)):
            text = alerts.nth(i).inner_text(timeout=1000).lower()
            if any(word in text for word in ("expired", "no longer", "closed", "unavailable")):
                return True
    except Exception:
        pass
    return False


def rule_answer(question: str, config: dict) -> str | None:
    q = normalize(question)
    candidate = config.get("candidate", {})
    answers = config.get("screening_answers", {})

    rules: list[tuple[str, Any]] = [
        (r"current ctc|current salary|present ctc|ctc.*current", answers.get("current_ctc")),
        (r"expected ctc|expected salary|desired ctc|ctc.*expect", answers.get("expected_ctc")),
        (r"notice period|serving notice|joining time", answers.get("notice_period")),
        (r"total experience|years of experience|work experience|experience", answers.get("experience")),
        (r"relocate|relocation|willing to relocate|open to relocate", answers.get("relocation")),
        (r"linkedin", candidate.get("linkedin")),
        (r"github", candidate.get("github")),
        (r"portfolio|personal website|website url", candidate.get("portfolio")),
        (r"email", candidate.get("email")),
        (r"phone|mobile|contact number|whatsapp", answers.get("phone")),
        (r"full.?name|your name", candidate.get("name")),
        (r"bond|service agreement", answers.get("bond", "No")),
        (r"work from home|remote|wfh", answers.get("remote", "Yes")),
        (r"hybrid", answers.get("hybrid", "Yes")),
        (r"full.?time|fulltime", answers.get("full_time", "Yes")),
        (r"authorized|legally authorized|work permit", answers.get("work_authorization", "Yes")),
        (r"passport|visa", answers.get("visa", "No")),
        (r"gender", answers.get("gender")),
        (r"date of birth|dob", answers.get("date_of_birth")),
        (r"current location|current city|where do you live", answers.get("current_location")),
        (r"preferred location", ", ".join(config.get("locations", []))),
    ]

    for pattern, value in rules:
        if value and re.search(pattern, q, re.I):
            return str(value)
    return None


def pick_radio_index(answer: str, options: list[dict[str, str]]) -> int:
    if not options:
        return 0

    answer_n = normalize(answer)
    for i, opt in enumerate(options):
        label_n = normalize(opt.get("label", ""))
        if answer_n == label_n or answer_n in label_n or label_n in answer_n:
            return i

    positive = ("yes", "immediate", "willing", "agree", "available", "open")
    negative = ("no", "not willing", "cannot", "unavailable")
    if any(p in answer_n for p in positive):
        for i, opt in enumerate(options):
            if re.search(r"\byes\b|immediate|willing|agree|available", opt.get("label", ""), re.I):
                return i
    if any(n in answer_n for n in negative):
        for i, opt in enumerate(options):
            if re.search(r"\bno\b|not willing|cannot", opt.get("label", ""), re.I):
                return i

    # Numeric answer like "1 Year" -> match option containing "1"
    numbers = re.findall(r"\d+", answer_n)
    if numbers:
        for i, opt in enumerate(options):
            if numbers[0] in normalize(opt.get("label", "")):
                return i

    return 0


def ai_answer(question: str, options: list[dict[str, str]], config: dict) -> str | None:
    api_key = config.get("settings", {}).get("gemini_api_key", "")
    if not api_key:
        return None

    try:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        profile = {
            "candidate": config.get("candidate", {}),
            "screening_answers": config.get("screening_answers", {}),
            "skills": config.get("skills", []),
        }
        options_text = "\n".join(
            f"{i + 1}. {opt.get('label', '')}" for i, opt in enumerate(options)
        )
        prompt = (
            "Answer this Naukri job application question using ONLY the candidate profile below. "
            "Be concise (1-5 words). For multiple choice, reply with ONLY the option number.\n\n"
            f"Profile: {profile}\n\nQuestion: {question}\n"
        )
        if options_text:
            prompt += f"Options:\n{options_text}\n"
        response = model.generate_content(prompt)
        return (response.text or "").strip()
    except Exception as exc:
        print(f"[questionnaire] AI fallback failed: {exc}", flush=True)
        return None


def resolve_answer(question: str, options: list[dict[str, str]], config: dict) -> str:
    answer = rule_answer(question, config)
    if answer:
        return answer

    ai = ai_answer(question, options, config)
    if ai:
        if options and ai.strip().isdigit():
            idx = int(ai.strip()) - 1
            if 0 <= idx < len(options):
                return options[idx].get("label", ai)
        return ai

    defaults = config.get("screening_answers", {})
    return defaults.get("default", "Yes")


def extract_chatbot_question(page: Page) -> str:
    selectors = [
        "li.botItem span",
        "[class*='botItem'] [class*='botMsg']",
        "[class*='chatbot'] [class*='bot']",
        "[class*='botItem']",
    ]
    for sel in selectors:
        loc = page.locator(sel)
        if loc.count():
            try:
                text = loc.last.inner_text(timeout=1500).strip()
                if text and len(text) > 2:
                    return text
            except Exception:
                continue
    return ""


def extract_radio_options(page: Page) -> list[dict[str, str]]:
    containers = page.locator("[class*='radio-btn'], .ssrc__radio-btn-container")
    options: list[dict[str, str]] = []
    for i in range(containers.count()):
        container = containers.nth(i)
        try:
            label = container.locator("label").first.inner_text(timeout=1000).strip()
            value = container.locator("input").first.get_attribute("value") or str(i)
            if label:
                options.append({"label": label, "value": value, "index": i})
        except Exception:
            continue
    return options


def click_chatbot_save(page: Page) -> bool:
    save_selectors = [
        "button:has-text('Save')",
        "button:has-text('Submit')",
        "button:has-text('Continue')",
        "[class*='sendMsg']",
        "[class*='save']",
    ]
    for sel in save_selectors:
        btn = page.locator(sel).filter(has_text=re.compile(r"save|submit|continue|send", re.I))
        if btn.count():
            try:
                btn.first.click(force=True, timeout=3000)
                page.wait_for_timeout(1200)
                return True
            except Exception:
                continue
    return False


def answer_radio_question(page: Page, answer: str, options: list[dict[str, str]]) -> bool:
    if not options:
        return False
    idx = pick_radio_index(answer, options)
    container = page.locator("[class*='radio-btn'], .ssrc__radio-btn-container").nth(idx)
    try:
        radio = container.locator("input[type='radio']").first
        radio.click(force=True, timeout=3000)
        page.wait_for_timeout(500)
        return True
    except Exception:
        try:
            container.locator("label").first.click(force=True, timeout=3000)
            return True
        except Exception:
            return False


def answer_text_question(page: Page, answer: str) -> bool:
    inputs = page.locator(
        "[class*='textArea'], textarea, input[type='text']:visible, [contenteditable='true']"
    )
    for i in range(min(inputs.count(), 5)):
        field = inputs.nth(i)
        try:
            if not field.is_visible():
                continue
            field.click(force=True)
            field.fill(answer)
            page.wait_for_timeout(400)
            return True
        except Exception:
            continue
    return False


def handle_questionnaire_step(page: Page, config: dict) -> bool:
    """Answer one chatbot step. Returns True if a question was handled."""
    if is_application_complete(page):
        return False

    options = extract_radio_options(page)
    question = extract_chatbot_question(page)

    if not question and not options:
        return False

    if not question and options:
        question = "Select the best option"

    answer = resolve_answer(question, options, config)
    print(f"[questionnaire] Q: {question[:80]} -> A: {answer[:60]}", flush=True)

    if options:
        if not answer_radio_question(page, answer, options):
            return False
    else:
        if not answer_text_question(page, answer):
            return False

    click_chatbot_save(page)
    page.wait_for_timeout(1500)
    return True


def complete_application_questionnaire(page: Page, config: dict, max_steps: int = 20) -> bool:
    """Loop through Naukri chatbot / screening steps until applied or stuck."""
    if is_job_unavailable(page):
        return False

    for step in range(max_steps):
        if is_application_complete(page):
            return True

        handled = False
        try:
            handled = handle_questionnaire_step(page, config)
        except PlaywrightTimeout:
            pass
        except Exception as exc:
            print(f"[questionnaire] step {step + 1} error: {exc}", flush=True)

        if is_application_complete(page):
            return True

        if not handled:
            break

    return is_application_complete(page)
