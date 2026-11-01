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


def test_in_scope_fix_push_does_not_authorize_deploy():
    text = _flat(AGENTS)
    assert "fast-forward push on the same PR branch" in text
    assert "confirmed in-scope review finding" in text
    assert "never covers force-push, history rewrite, a wider scope, deploy, or a production change" in text


def test_cleanup_follows_owner_ruling():
    text = _flat(AGENTS)
    assert "## Workspace cleanup when finishing" in text
    assert "archive reusable content first, then delete merged or obsolete local branches and worktrees" in text
    assert "Owner ruling, 2026-10-15" in text
    assert "## Git Branches" in text
    assert "only when the corresponding local and" not in text
    assert "clean up the source branch locally and on GitHub" not in text
    assert "does not perform that deletion" not in text
    assert "Otherwise delete them" not in text


def test_contributing_keeps_wrong_comments_advisory():
    text = _flat(CONTRIBUTING)
    assert "not silently obeyed" in text
    assert "[AGENTS.md](AGENTS.md)" in text
    assert "need their own authorization" in text
