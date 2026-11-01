"""The dispatch tables cover the error taxonomy exactly.

An error code with no place to appear is an error the owner never sees. That
used to be a review question - somebody had to notice, while reading a view,
that one branch of a match had been left out. These tests turn it into a
failing suite: the three tables in ui/error_presentation.py must have exactly
the key set of errors.MESSAGE_BY_CODE, no code missing and none invented, and
every value must come from the small vocabularies the views know how to draw.

Set EQUALITY rather than containment is deliberate. Containment would pass a
table that had grown a key for a code that no longer exists, and that stale
row is how a renamed code ends up dispatched under two names at once.

This module imports no Qt: error_presentation holds classifications only, so
the check runs without a display or an event loop.
"""

from __future__ import annotations

from wuwaterm_client import errors, strings
from wuwaterm_client.ui import error_presentation

ALL_CODES = frozenset(errors.MESSAGE_BY_CODE)


def test_every_code_has_a_severity_and_no_table_row_is_stale() -> None:
    assert frozenset(error_presentation.SEVERITY_BY_CODE) == ALL_CODES


def test_every_code_has_a_surface_and_no_table_row_is_stale() -> None:
    assert frozenset(error_presentation.SURFACE_BY_CODE) == ALL_CODES


def test_every_code_has_an_action_entry_and_no_table_row_is_stale() -> None:
    # `None` is a decision here, not an omission: three codes are fixed by
    # editing the text in the field, so there is nothing to offer a button
    # for. The key must still exist.
    assert frozenset(error_presentation.ACTION_BY_CODE) == ALL_CODES


def test_severities_come_from_the_vocabulary_the_banner_can_draw() -> None:
    for code, severity in error_presentation.SEVERITY_BY_CODE.items():
        assert severity in error_presentation.VALID_SEVERITIES, code


def test_surfaces_come_from_the_vocabulary_the_window_actually_has() -> None:
    for code, surface in error_presentation.SURFACE_BY_CODE.items():
        assert surface in error_presentation.VALID_SURFACES, code


def test_actions_come_from_the_vocabulary_the_views_can_perform() -> None:
    for code, action in error_presentation.ACTION_BY_CODE.items():
        assert action is None or action in error_presentation.VALID_ACTIONS, code


def test_every_offered_action_has_wording() -> None:
    """An action with no label is a blank button."""
    for action in error_presentation.VALID_ACTIONS:
        label = error_presentation.ACTION_LABEL_BY_ACTION[action]
        assert label.strip()


def test_the_cancelled_code_is_never_dressed_as_a_failure() -> None:
    """Cancelling is something the owner asked for. It is a confirmation,
    and confirmations do not go red or ask to be retried."""
    presentation = error_presentation.presentation_for(errors.ERROR_CANCELLED)

    assert presentation.severity == error_presentation.SEVERITY_MUTED
    assert presentation.surface == error_presentation.SURFACE_STATUS
    assert presentation.action is None


def test_a_resolved_presentation_carries_its_own_message_and_label() -> None:
    presentation = error_presentation.presentation_for(errors.ERROR_NOT_CONFIGURED)

    assert presentation.code == errors.ERROR_NOT_CONFIGURED
    assert presentation.message == strings.ERROR_MSG_NOT_CONFIGURED
    assert presentation.action == error_presentation.ACTION_OPEN_SETTINGS
    assert presentation.action_label == strings.ACTION_OPEN_SETTINGS


def test_a_code_the_tables_never_heard_of_still_reaches_the_owner() -> None:
    """Falling back is not the same as being dropped: an unrecognised code
    keeps its own identity and borrows the `unknown` row to be shown with."""
    presentation = error_presentation.presentation_for("a_code_from_the_future")

    assert presentation.code == "a_code_from_the_future"
    assert presentation.severity in error_presentation.VALID_SEVERITIES
    assert presentation.surface == error_presentation.SURFACE_BANNER
    assert presentation.message == strings.ERROR_MSG_UNKNOWN


def test_the_three_refusals_do_not_read_the_same() -> None:
    """Each sends the owner somewhere different - to Settings to enter an
    address, to Settings to replace an unusable one, or nowhere at all
    because the client does not know what happened. Identical wording would
    collapse three destinations into one."""
    messages = {
        strings.ERROR_MSG_NOT_CONFIGURED,
        strings.ERROR_MSG_UNKNOWN,
        strings.ERROR_MSG_INSECURE_ENDPOINT,
    }

    assert len(messages) == 3


def test_every_message_in_the_taxonomy_is_distinct() -> None:
    """Two codes with the same words are two codes the owner cannot tell
    apart, whatever the tables say about where they appear."""
    messages = list(errors.MESSAGE_BY_CODE.values())

    assert len(set(messages)) == len(messages)


# -- Codex P2 回归门(PR #63 评审发现) --------------------------------------


def test_a_non_finite_score_is_refused_by_the_parser() -> None:
    """NaN 不能穿过解析层进到部件里。

    Python 的 json 会接受 NaN(它不是 JSON,但解析器照收),而它能通过原来的
    类型检查;真正炸的地方是绘制期的 round(),那已经在「把不可用响应变成
    ClientError」的包装之外,于是查词任务以未处理异常收场而不是一条内联错误。
    """
    import math

    import pytest as _pytest

    from wuwaterm_client.api import TermsResult

    for bad in (float("nan"), float("inf"), float("-inf")):
        payload = {
            "query": "x",
            "matches": [
                {"zh": "词", "en": "term", "category": "item",
                 "score": bad, "reason": "exact"}
            ],
            "request_id": "rid",
        }
        with _pytest.raises(ValueError):
            TermsResult.from_json(payload)
        assert not math.isfinite(bad)
