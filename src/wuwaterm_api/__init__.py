"""HTTP adapter for the wuwaterm translation service.

A separate top-level package beside ``wuwaterm``: it is one more inbound
adapter, not part of the Telegram bot. It may only reach the shared
application layer (``wuwaterm.application``) plus the protocol-neutral
helpers ``wuwaterm.models``, ``wuwaterm.translation_policy``,
``wuwaterm.logging_utils`` and ``wuwaterm.request_trace``. That narrow
allowlist is enforced by ``scripts/check_architecture_boundaries.py`` and is
what guarantees the API cannot bypass the shared translation pipeline.
(``request_trace`` is on it as an instrument rather than a way in: it holds
no dictionary, pipeline or transport, and the stages it times are opened by
the core.)
"""

from __future__ import annotations

__all__ = ["API_VERSION", "TERM_QUERY_MAX_LENGTH"]

API_VERSION = "v1"

# Both presentation layers in this package (the JSON routes and the web
# views) must reject over-long term queries at the same length, so the
# constant lives here rather than in either layer. A previous local copy in
# the web layer (120) disagreed with the JSON layer (200) - the exact drift
# that importing instead of restating is meant to prevent.
TERM_QUERY_MAX_LENGTH = 200
