import csv
import json
from datetime import datetime
from pathlib import Path


class ApplicationLogger:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.applied_path = self.data_dir / "applied_jobs.json"
        self.csv_path = self.data_dir / "applications_log.csv"
        self.applied = self._load_applied()
        self._ensure_csv_header()

    def _load_applied(self) -> set[str]:
        if not self.applied_path.exists():
            return set()
        data = json.loads(self.applied_path.read_text(encoding="utf-8"))
        return set(data.get("keys", []))

    def _save_applied(self) -> None:
        self.applied_path.write_text(
            json.dumps({"keys": sorted(self.applied)}, indent=2),
            encoding="utf-8",
        )

    def _ensure_csv_header(self) -> None:
        if self.csv_path.exists():
            return
        with self.csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Company", "Role", "Link", "Status", "Match Score"])

    def job_key(self, job_id: str, company: str, role: str) -> str:
        return f"{job_id}|{normalize_key(company)}|{normalize_key(role)}"

    def is_duplicate(self, job_id: str, company: str, role: str) -> bool:
        return self.job_key(job_id, company, role) in self.applied

    def log_application(
        self,
        company: str,
        role: str,
        link: str,
        status: str,
        match_score: float,
        job_id: str = "",
    ) -> None:
        key = self.job_key(job_id, company, role)
        self.applied.add(key)
        self._save_applied()

        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                company,
                role,
                link,
                status,
                match_score,
            ])


def normalize_key(value: str) -> str:
    return " ".join((value or "").lower().split())
