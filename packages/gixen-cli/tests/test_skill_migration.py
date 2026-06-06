"""Static validation tests for the comic-pipeline skill layout.

Originally written for the PER-31/32/33 standalone-repo migration; rewritten in
BUI-65 for the monorepo reality. The skill bodies now live in
`.claude/commands/comic/` (the `skills/` symlink points there), the eBay/ezship
tools live under `apps/`, and there is no Brain-vault symlink — so the paths are
anchored to this repo via `__file__` rather than `~/Projects/comic-pipeline`,
and the obsolete Brain-vault assertions are gone.
"""

import re
from pathlib import Path

import pytest

# tests → gixen-cli → packages → repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = REPO_ROOT / ".claude" / "commands" / "comic"
EBAY_FETCH_SCRIPT = REPO_ROOT / "apps" / "ebay" / "src" / "ebay_fetch.py"
EZSHIP_CLI = REPO_ROOT / "apps" / "ezship" / "src" / "cli.ts"

EXPECTED_SKILLS = [
    "buy.md",
    "collection-add.md",
    "collection-check.md",
    "ezship-add.md",
    "fmv.md",
    "grade.md",
    "identify.md",
    "seller-scan.md",
    "snipe-add.md",
    "snipe-show.md",
    "verify.md",
    "wishlist-add.md",
]

STALE_PATH_PATTERNS = [
    r"~/Projects/ebay-cli",
    r"~/Projects/ebay-sniper",
    r"~/Projects/ezship-cli",
    r"Brain v3\.0/.claude/commands/comic",
]


def _require_skills_dir():
    if not SKILLS_DIR.is_dir():
        pytest.skip(f"comic skills dir not found at {SKILLS_DIR}")


# --- R1: All expected skill files exist ---

def test_all_skills_present():
    _require_skills_dir()
    missing = [s for s in EXPECTED_SKILLS if not (SKILLS_DIR / s).is_file()]
    assert missing == [], f"Missing skill files in {SKILLS_DIR}: {missing}"


# --- R3: No stale path references ---

@pytest.mark.parametrize("skill_name", EXPECTED_SKILLS)
def test_no_stale_paths_in_skill(skill_name):
    _require_skills_dir()
    content = (SKILLS_DIR / skill_name).read_text()
    for pattern in STALE_PATH_PATTERNS:
        assert not re.search(pattern, content), (
            f"{skill_name} still contains stale path matching '{pattern}'"
        )


# --- R4: Tool scripts exist at new paths ---

def test_ebay_fetch_script_exists():
    _require_skills_dir()
    assert EBAY_FETCH_SCRIPT.is_file(), (
        f"ebay_fetch.py not found at {EBAY_FETCH_SCRIPT}"
    )


def test_ezship_cli_exists():
    _require_skills_dir()
    assert EZSHIP_CLI.is_file(), (
        f"cli.ts not found at {EZSHIP_CLI}"
    )


# --- R5: Credential key alignment (grade.md vs ebay_fetch.py) ---

def test_ebay_fetch_uses_client_id_key():
    _require_skills_dir()
    content = EBAY_FETCH_SCRIPT.read_text()
    assert 'cfg.get("client_id")' in content or "client_id" in content, (
        "ebay_fetch.py does not reference 'client_id' — grade.md credential alignment may be wrong"
    )


def test_grade_md_reads_json_not_dotenv():
    _require_skills_dir()
    content = (SKILLS_DIR / "grade.md").read_text()
    assert "load_dotenv" not in content, (
        "grade.md still uses load_dotenv() — should use json.load() for config.json"
    )
    assert "json.load" in content, (
        "grade.md does not use json.load() to read config.json"
    )
    assert "client_id" in content, (
        "grade.md does not reference 'client_id' key — may be using old EBAY_APP_ID naming"
    )
