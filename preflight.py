"""Startup validation before the agent runs."""

from __future__ import annotations

from pathlib import Path


def validate_startup(config: dict, base_dir: Path, resume_path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if not resume_path.exists():
        errors.append(f"Resume not found: {resume_path} — place your PDF at this path.")

    phone = config.get("screening_answers", {}).get("phone", "")
    if not phone or "X" in phone.upper() or len(phone) < 10:
        warnings.append("Phone number in screening_answers looks missing or invalid.")

    if not config.get("target_roles"):
        errors.append("target_roles is empty in config.json.")

    if not config.get("locations"):
        errors.append("locations is empty in config.json.")

    candidate = config.get("candidate", {})
    for field in ("name", "email"):
        if not candidate.get(field):
            warnings.append(f"candidate.{field} is not set.")

    settings = config.get("settings", {})
    if settings.get("browser_mode") == "cdp":
        warnings.append(
            "CDP mode: run .\\start_chrome.ps1 first and stay logged into Naukri in Chrome."
        )

    return errors, warnings


def print_preflight_results(errors: list[str], warnings: list[str]) -> bool:
    for warning in warnings:
        print(f"[preflight:warn] {warning}", flush=True)
    for error in errors:
        print(f"[preflight:error] {error}", flush=True)
    if errors:
        print("[preflight] Fix the errors above before running.", flush=True)
        return False
    if not warnings:
        print("[preflight] All checks passed.", flush=True)
    return True
