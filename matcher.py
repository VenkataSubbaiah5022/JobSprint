import re
from typing import Iterable


def normalize(text: str) -> str:
    text = re.sub(r"[^\w\s.+#]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def normalize_role(title: str) -> str:
    text = normalize(title)
    text = text.replace("full stack", "fullstack")
    text = text.replace("front end", "frontend")
    text = text.replace("back end", "backend")
    return text


def tokenize_skills(text: str) -> set[str]:
    parts = re.split(r"[,;/|•\n]+", text or "")
    return {normalize(p) for p in parts if normalize(p)}


def role_match_score(title: str, target_roles: Iterable[str]) -> float:
    title_n = normalize_role(title)
    best = 0.0
    for role in target_roles:
        role_n = normalize_role(role)
        if role_n in title_n or title_n in role_n:
            return 100.0
        role_tokens = set(role_n.split())
        title_tokens = set(title_n.split())
        overlap = len(role_tokens & title_tokens) / max(len(role_tokens), 1)
        best = max(best, overlap * 100)
    return best


def is_strong_role_match(title: str, config: dict) -> bool:
    threshold = config["filters"].get("strong_role_match_threshold", 80)
    return role_match_score(title, config["target_roles"]) >= threshold


def should_apply_to_job(job: dict, config: dict, *, preview: bool = True) -> bool:
    title = job.get("title", "")
    if is_title_excluded(title, config["exclude_keywords"]):
        return False
    if is_strong_role_match(title, config):
        return True
    score = calculate_match_score(job, config, preview=preview)
    return score >= config["filters"]["min_match_score"]


def skills_match_score(jd_text: str, profile_skills: Iterable[str]) -> float:
    jd_n = normalize(jd_text)
    profile = [normalize(s) for s in profile_skills]
    if not profile:
        return 0.0
    hits = sum(1 for skill in profile if skill in jd_n)
    return (hits / len(profile)) * 100


def location_match_score(location: str, preferred: Iterable[str]) -> float:
    loc_n = normalize(location)
    if not loc_n:
        return 50.0
    for pref in preferred:
        if normalize(pref) in loc_n:
            return 100.0
    if "remote" in loc_n or "hybrid" in loc_n or "work from home" in loc_n:
        return 90.0
    return 0.0


def _has_exclude_keyword(text: str, keyword: str) -> bool:
    kw = normalize(keyword)
    text_n = normalize(text)
    if not kw or not text_n:
        return False
    if " " in kw or "-" in kw:
        return kw in text_n
    return re.search(rf"\b{re.escape(kw)}\b", text_n) is not None


def is_title_excluded(title: str, exclude_keywords: Iterable[str]) -> bool:
    title_n = normalize(title)
    return any(_has_exclude_keyword(title_n, kw) for kw in exclude_keywords)


def exclude_penalty(
    title: str,
    jd_text: str,
    exclude_keywords: Iterable[str],
    *,
    check_jd: bool = True,
) -> float:
    title_n = normalize(title)
    jd_n = normalize(jd_text) if check_jd else ""
    penalty = 0.0
    for kw in exclude_keywords:
        if _has_exclude_keyword(title_n, kw):
            penalty += 35.0
            continue
        if jd_n and _has_exclude_keyword(jd_n, kw):
            penalty += 35.0
    return min(penalty, 100.0)


def experience_match_score(jd_text: str, exp_min: int, exp_max: int) -> float:
    jd_n = normalize(jd_text)
    numbers = [int(n) for n in re.findall(r"(\d+)\s*(?:\+)?\s*(?:years?|yrs?)", jd_n)]
    if not numbers:
        return 70.0
    req = max(numbers)
    if req <= exp_max and req >= exp_min:
        return 100.0
    if req <= exp_max + 1:
        return 75.0
    return 20.0


def salary_match_score(salary_text: str, min_lpa: float, max_lpa: float) -> float:
    text = normalize(salary_text)
    if not text or "not disclosed" in text or "undisclosed" in text:
        return 80.0
    values = [float(v) for v in re.findall(r"(\d+(?:\.\d+)?)", text)]
    if not values:
        return 70.0
    low, high = min(values), max(values)
    if high < min_lpa:
        return 30.0
    if low > max_lpa:
        return 40.0
    return 100.0


def calculate_match_score(job: dict, config: dict, *, preview: bool = False) -> float:
    title = job.get("title", "")
    jd = " ".join([
        job.get("description", ""),
        job.get("skills", ""),
        job.get("experience", ""),
        job.get("listing_snippet", ""),
    ])
    location = job.get("location", "")
    salary = job.get("salary", "")
    filters = config["filters"]

    if is_title_excluded(title, config["exclude_keywords"]):
        return 0.0

    if not preview and exclude_penalty(title, jd, config["exclude_keywords"], check_jd=True) >= 35:
        return 0.0

    role_score = role_match_score(title, config["target_roles"])
    if role_score < 40:
        return round(role_score * 0.3, 1)

    raw_skills = skills_match_score(jd, config["skills"])
    if role_score >= 80:
        raw_skills = max(raw_skills, 35.0)

    scores = {
        "role": role_score * 0.30,
        "skills": raw_skills * 0.35,
        "location": location_match_score(location, config["locations"]) * 0.15,
        "experience": experience_match_score(jd, filters["experience_min"], filters["experience_max"]) * 0.10,
        "salary": salary_match_score(salary, filters["salary_min_lpa"], filters["salary_max_lpa"]) * 0.10,
    }
    total = round(sum(scores.values()), 1)
    if role_score >= 100:
        total = max(total, 55.0)
    return total
