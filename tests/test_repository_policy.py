import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
FORBIDDEN_SUFFIXES = {".dll", ".exe", ".sys", ".so"}
FORBIDDEN_TEXT = ("authorization: " + "xbl3.0 x=", "proof_" + "private_key=")


def test_repository_contains_no_prohibited_binaries_or_live_secrets():
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    ).decode().split("\0")
    tracked = [ROOT / name for name in names if name]
    assert not [path for path in tracked if path.suffix.lower() in FORBIDDEN_SUFFIXES]
    findings = []
    for path in tracked:
        if path.relative_to(ROOT).parts[:2] == ("docs", "superpowers"):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        text = path.read_text(errors="ignore").lower()
        findings.extend((path, token) for token in FORBIDDEN_TEXT if token in text)
    assert findings == []


def test_declared_licenses_exist():
    assert (ROOT / "LICENSES/GPL-3.0-or-later.txt").is_file()
    assert (ROOT / "LICENSES/LGPL-2.1-or-later.txt").is_file()
