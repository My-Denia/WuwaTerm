"""Shared policy constants for manual and linked-channel translation."""

from .sentence import BUDGET_EXHAUSTED_NOTICE, TRANSLATION_UNAVAILABLE_NOTICE


LLM_INPUT_CHAR_LIMIT = 2000
LLM_FAILURE_NOTICES = frozenset(
    (BUDGET_EXHAUSTED_NOTICE, TRANSLATION_UNAVAILABLE_NOTICE)
)
# Protected-text / HTML-mode failures caused by the response CONTENT (blank
# output, broken term/tag placeholders), not by transport/quota. These are worth
# one plain retry: the plain prompt has no structural placeholders to break,
# while a timeout/budget/upstream error would just fail again immediately.
HTML_CONTENT_FAILURE_REASONS = frozenset(
    {"invalid_response", "html_integrity", "protected_text_integrity"}
)
