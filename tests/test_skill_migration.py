"""Static validation tests for the PER-31/32/33 comic-pipeline migration."""

import os
import re
from pathlib import Path

import pytest

COMIC_PIPELINE = Path.home() / "Projects" / "comic-pipeline"
SKILLS_DIR = COMIC_PIPELINE / "skills"
BRAIN_VAULT_COMIC = Path.home() / "Projects" / "Brain v3.0" / ".claude" / "commands" / "comic"
EBAY_FETCH_SCRIPT = COMIC_PIPELINE / "apps" / "ebay" / "src" / "ebay_fetch.py"
EZSHIP_CLI = COMIC_PIPELINE / "apps" / "ezship" / "src" / "cli.ts"

EXPECTED_SKILLS = [
    "buy.md",
    "collection-add.md",
    "collection-check.md",
    "ezship-add.md",
    "fmv.md",
    "grade.md",
    "identify.md",
    "snipe-add.md",
    "snipe-show.md",
    "verify.md",
]

STALE_PATH_PATTERNS = [
    r"~/Projects/ebay-cli",
    r"~/Projects/ebay-sniper",
    r"~/Projects/ezship-cli",
    r"Brain v3\.0/.claude/commands/comic",
]


def _require_comic_pipeline():
    if not COMIC_PIPELINE.exists():
        pytest.skip(f"comic-pipeline repo not found at {COMIC_PIPELINE}")


# --- R1: All 9 skill files exist ---

def test_all_skills_present():
    _require_comic_pipeline()
    missing = [s for s in EXPECTED_SKILLS if not (SKILLS_DIR / s).is_file()]
    assert missing == [], f"Missing skill files in {SKILLS_DIR}: {missing}"


# --- R2: Brain vault symlink ---

def test_brain_vault_is_symlink():
    _require_comic_pipeline()
    assert BRAIN_VAULT_COMIC.is_symlink(), (
        f"{BRAIN_VAULT_COMIC} is not a symlink — expected it to point at comic-pipeline/skills/"
    )


def test_brain_vault_symlink_resolves_to_skills():
    _require_comic_pipeline()
    if not BRAIN_VAULT_COMIC.is_symlink():
        pytest.skip("Brain vault path is not a symlink")
    resolved = BRAIN_VAULT_COMIC.resolve()
    expected = SKILLS_DIR.resolve()
    assert resolved == expected, (
        f"Symlink target mismatch: {resolved} != {expected}"
    )


def test_brain_vault_symlink_exposes_all_skills():
    _require_comic_pipeline()
    if not BRAIN_VAULT_COMIC.is_symlink():
        pytest.skip("Brain vault path is not a symlink")
    via_symlink = {f.name for f in BRAIN_VAULT_COMIC.iterdir() if f.suffix == ".md"}
    expected = set(EXPECTED_SKILLS)
    assert via_symlink == expected, (
        f"Symlink exposes different files than expected.\n"
        f"Extra: {via_symlink - expected}\n"
        f"Missing: {expected - via_symlink}"
    )


# --- R3: No stale path references ---

@pytest.mark.parametrize("skill_name", EXPECTED_SKILLS)
def test_no_stale_paths_in_skill(skill_name):
    _require_comic_pipeline()
    content = (SKILLS_DIR / skill_name).read_text()
    for pattern in STALE_PATH_PATTERNS:
        assert not re.search(pattern, content), (
            f"{skill_name} still contains stale path matching '{pattern}'"
        )


# --- R4: Tool scripts exist at new paths ---

def test_ebay_fetch_script_exists():
    _require_comic_pipeline()
    assert EBAY_FETCH_SCRIPT.is_file(), (
        f"ebay_fetch.py not found at {EBAY_FETCH_SCRIPT}"
    )


def test_ezship_cli_exists():
    _require_comic_pipeline()
    assert EZSHIP_CLI.is_file(), (
        f"cli.ts not found at {EZSHIP_CLI}"
    )


# --- R5: Credential key alignment (grade.md vs ebay_fetch.py) ---

def test_ebay_fetch_uses_client_id_key():
    _require_comic_pipeline()
    content = EBAY_FETCH_SCRIPT.read_text()
    assert 'cfg.get("client_id")' in content or "client_id" in content, (
        "ebay_fetch.py does not reference 'client_id' — grade.md credential alignment may be wrong"
    )


def test_grade_md_reads_json_not_dotenv():
    _require_comic_pipeline()
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
