"""The tracked review rules stay where GitHub Codex Review reads them."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_code_review_rules_are_tracked_at_the_root():
    text = _flat(AGENTS)
    assert "## Code Review Rules" in text
    assert "Severity and scope are separate questions." in text
    assert "A severity label does not decide scope." in text
    assert "record it as a follow-up" in text
    assert "stop the one-by-one patch treadmill" in text
    assert "not an instruction to change the pull request" in text


def test_in_scope_fix_push_does_not_authorize_merge_or_deploy():
    text = _flat(AGENTS)
    assert "## Pull request fix push" in text
    assert "fast-forward push" in text
    assert "same branch" in text
    assert "force-push" in text
    assert "merge, deploy" in text
    assert "production database refresh" in text
    assert "outside the original scope is not a reason" in text


def test_contributing_keeps_wrong_comments_advisory():
    text = _flat(CONTRIBUTING)
    assert "not silently obeyed" in text
    assert "[AGENTS.md](AGENTS.md)" in text
    assert "need their own authorization" in text
