"""آپدیت خودکار از گیت‌هاب: fetch -> merge --ff-only -> نصب پکیج‌ها -> تست سینتکس -> (در صورت خطا) rollback."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _branch() -> str:
    return os.environ.get("GIT_BRANCH", "main")


def _git(*args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout
    )


def is_git_repo() -> bool:
    return (ROOT / ".git").exists()


def current_version() -> str:
    try:
        r = _git("rev-parse", "--short", "HEAD", timeout=10)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def check_and_update() -> tuple[str, str]:
    """خروجی: (status, message) — status یکی از none / updated / error"""
    if not is_git_repo():
        return "error", "این پوشه یک ریپوی git نیست (با git clone نصب کن)."
    branch = _branch()
    try:
        f = _git("fetch", "origin", branch)
        if f.returncode != 0:
            return "error", f"git fetch ناموفق بود:\n{f.stderr.strip()}"

        local = _git("rev-parse", "HEAD").stdout.strip()
        remote = _git("rev-parse", f"origin/{branch}").stdout.strip()
        if not remote:
            return "error", f"برنچ origin/{branch} پیدا نشد."
        if local == remote:
            return "none", "نسخه به‌روز است."

        changed = _git("diff", "--name-only", local, remote).stdout.split()
        log = _git("log", "--oneline", f"{local}..{remote}").stdout.strip()

        m = _git("merge", "--ff-only", f"origin/{branch}")
        if m.returncode != 0:
            return "error", f"merge ناموفق بود (تغییر محلی روی فایل‌ها هست؟):\n{m.stderr.strip()}"

        if "requirements.txt" in changed:
            p = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                cwd=ROOT, capture_output=True, text=True, timeout=600,
            )
            if p.returncode != 0:
                _git("reset", "--hard", local)
                return "error", f"نصب پکیج‌ها ناموفق بود، به نسخه قبل برگشتم:\n{p.stderr[-500:]}"

        py_files = [str(p.relative_to(ROOT)) for p in ROOT.glob("*.py")]
        c = subprocess.run(
            [sys.executable, "-m", "py_compile", *py_files],
            cwd=ROOT, capture_output=True, text=True,
        )
        if c.returncode != 0:
            _git("reset", "--hard", local)
            return "error", f"کد جدید خطای سینتکس دارد، به نسخه قبل برگشتم:\n{c.stderr[-500:]}"

        return "updated", log or "به‌روزرسانی انجام شد."
    except subprocess.TimeoutExpired:
        return "error", "زمان اجرای دستور git تمام شد."
    except Exception as e:  # noqa: BLE001
        return "error", f"خطای غیرمنتظره: {e}"


def restart() -> None:
    """ریستارت کامل پروسه (با systemd هم، چون PID عوض نمی‌شود، مشکلی ندارد)."""
    os.execv(sys.executable, [sys.executable, *sys.argv])
