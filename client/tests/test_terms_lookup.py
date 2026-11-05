"""What the lookup area adds on top of the behaviours it inherited.

The inherited ones - submit-driven requests, the in-flight guard, a cleared
table on failure and on an address change - live in
``test_view_parity_with_main.py``, which states them as a list derived from
the previous implementation. This file holds only what the redesign
introduced and is answerable for on its own:

* refusing to send when there is no address, and saying so in the area rather
  than repeating the window's setup checklist;
* refusing to send a SENTENCE, and handing it to the area that can translate
  it instead of answering with an empty dictionary table;
* the request-id row's three states - shown for any completed request, absent
  when no request has happened at all.

None of the three needs a timer, which is why they survived the withdrawal of
search-as-you-type: each is decided at the moment the owner submits.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from wuwaterm_client import strings  # noqa: E402
from wuwaterm_client.api import ApiClient, TermsResult  # noqa: E402
from wuwaterm_client.errors import ClientError  # noqa: E402
from wuwaterm_client.ui import terms_view as terms_view_module  # noqa: E402
from wuwaterm_client.ui.terms_view import TermsView  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _Service:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("q", "")
        self.queries.append(query)
        return httpx.Response(
            200,
            json={"query": query, "matches": [], "request_id": f"req-{query}"},
        )


def _client(handler, base_url: "str | None" = "https://test") -> ApiClient:
    return ApiClient(
        base_url,
        _test_transport=httpx.MockTransport(handler),
        token_provider=lambda: "wtd1.device.secret",
    )


def _card_action(card) -> QPushButton:
    buttons = card._action_host.findChildren(QPushButton)
    assert len(buttons) == 1, "the card must offer exactly one action here"
    return buttons[0]


def test_a_client_with_no_address_asks_nothing(qapp) -> None:
    """Submitting into an area that has nowhere to send anything is refused
    here as well as by the API client, so the screen stays quiet."""
    service = _Service()
    view = TermsView(_client(service, base_url=None))

    assert view.search_button.isEnabled() is False
    assert view.search_button.toolTip() == strings.TOOLTIP_NEEDS_ENDPOINT

    view.query_edit.setText("Jinhsi")
    view._on_search_clicked()

    assert service.queries == []
    # The area says what the area would show. It deliberately does NOT repeat
    # the window's setup checklist, which is on screen directly above it - one
    # heading drawn twice reads as one card drawn twice.
    assert view.empty_card.title_text == strings.EMPTY_TERMS_UNCONFIGURED_TITLE
    assert view.empty_card.title_text != strings.SETUP_STEPS_TITLE


def test_a_sentence_is_not_a_term_lookup(qapp) -> None:
    """An empty table would have said the dictionary has no such term. What
    is true is that this text was never a term."""
    service = _Service()
    view = TermsView(_client(service))

    long_query = "今" * (terms_view_module.MAX_TERM_LENGTH + 1)
    view.query_edit.setText(long_query)
    view._on_search_clicked()

    assert service.queries == []
    assert view.table.rowCount() == 0
    assert view.empty_card.title_text == strings.TERMS_SENTENCE_HINT_TITLE
    assert view.empty_card.subtitle_text == strings.TERMS_SENTENCE_HINT_SUBTITLE

    # The button restates a SHORTENED query, but what it carries onward is the
    # whole of it: the owner would otherwise get a truncated source text in
    # the translation area.
    carried: list[str] = []
    view.translate_requested.connect(carried.append)
    _card_action(view.empty_card).click()
    assert carried == [long_query]

    # A line break makes it a sentence at any length, and it is read on the
    # raw text - stripping first would let a trailing one through as a term.
    view.query_edit.setText("今汐\n")
    view._on_search_clicked()

    assert service.queries == []
    assert view.empty_card.title_text == strings.TERMS_SENTENCE_HINT_TITLE


def test_the_sentence_card_does_not_inherit_the_last_failure(qapp) -> None:
    """A refusal decided locally must not wear a previous request's error.

    The sentence branch never starts a request, so it never reaches the place
    every other path clears the banner. Without an explicit clear, the failure
    reported for the PREVIOUS text stays on screen beside the sentence card -
    an error attributed to text that was never sent anywhere.
    """
    service = _Service()
    view = TermsView(_client(service))

    view._render_error(ClientError("llm_unavailable", request_id="req-fail"))
    assert view.banner.is_showing() is True, "fixture failed to put a failure on screen"

    view.query_edit.setText("今" * (terms_view_module.MAX_TERM_LENGTH + 1))
    view._on_search_clicked()

    assert view.empty_card.title_text == strings.TERMS_SENTENCE_HINT_TITLE
    assert view.banner.is_showing() is False, "the sentence card kept the old failure"
    assert view.field_error.is_showing() is False
    assert service.queries == []


def test_a_field_error_does_not_survive_into_the_sentence_card(qapp) -> None:
    """The same rule for the field-level surface, and it has to be the SENTENCE
    branch that clears it.

    The text is put in place BEFORE the failure is rendered, and never edited
    afterwards. An earlier version of this test typed the sentence after the
    error, which meant the typing handler cleared the field error on the way -
    so the assertion passed with the sentence branch's own clear deleted. It
    was proving the wrong mechanism, and the mutation harness said so.

    The sequence here is a real one: a query is rejected as too long, and the
    owner presses Search again without changing it.
    """
    service = _Service()
    view = TermsView(_client(service))

    view.query_edit.setText("今汐\n")
    view._render_error(ClientError("input_too_long", request_id="req-field"))
    assert view.field_error.is_showing() is True

    view._on_search_clicked()

    assert view.field_error.is_showing() is False
    assert view.empty_card.title_text == strings.TERMS_SENTENCE_HINT_TITLE


def test_editing_the_query_drops_a_stale_field_rejection(qapp) -> None:
    """A red outline must not outlive the text that earned it.

    `input_too_long` and its two siblings mark the query box itself invalid.
    That verdict is about one particular value; once the owner types a
    different one, leaving it up claims the service rejected something it has
    never seen. The translation area's editor has had this handler all along.

    The second half of this test is the one that matters over time: the
    handler must clear and start NOTHING. Searching from here is what was
    withdrawn, and it needed a debounce, an ordering guard and an
    invalidation path that no longer exist.
    """
    service = _Service()
    view = TermsView(_client(service))

    view._render_error(ClientError("input_too_long", request_id="req-field"))
    assert view.field_error.is_showing() is True
    assert view.query_edit.property("invalid") is True

    view.query_edit.setText("今汐")

    assert view.field_error.is_showing() is False, "拒绝仍挂在一段没被拒绝过的文字上"
    assert view.query_edit.property("invalid") is not True
    assert service.queries == [], "输入触发了请求——输入即搜已经撤掉"


def test_the_request_id_survives_an_empty_or_failed_lookup(qapp) -> None:
    """每一次完成的请求都要留下可以拿去问运营方的句柄。

    请求 ID 行原本嵌在结果区里,而空结果与失败都会把结果区整块隐藏 —— 于是
    最可能被追问的那一类结局,恰好是唯一看不到 ID 的。
    """
    view = TermsView(_client(_Service()))

    result = TermsResult(query="无此词", matches=(), request_id="req-empty")
    view._render_result(result)

    assert view._request_id == "req-empty"
    assert view.diagnostics_button.isVisibleTo(view)
    view.diagnostics_button.click()
    assert view._request_id_row.isVisibleTo(view) is True, "空结果把请求 ID 行一起藏了"
    assert "req-empty" in view._request_id_label.text()


def test_no_request_id_row_before_any_lookup(qapp) -> None:
    """没发生过请求的屏幕上不该有请求 ID 行。

    与 Banner 的三态语义一致:失败或空结果**要**显示该行(带占位符),因为那是
    最可能被追问的结局;而首次绘制、清空输入、未配置这些从未发出请求的状态,
    没有「缺失的 ID」可言 —— 印一行「请求 ID:—」等于请人去问一次本客户端从未
    发出过的调用。
    """
    view = TermsView(_client(_Service()))

    # 首次绘制:没有请求发生过。
    assert view._request_id_row.isVisibleTo(view) is False

    # 有请求、但没拿到 ID:该行出现,带占位符。
    view._apply_request_id(None)
    assert view.diagnostics_button.isVisibleTo(view)
    view.diagnostics_button.click()
    assert view._request_id_row.isVisibleTo(view) is True
    assert strings.REQUEST_ID_PLACEHOLDER in view._request_id_label.text()

    # 回到空闲态(输入被清空)——又变成「没有请求」。
    view._show_idle_state()
    assert view._request_id_row.isVisibleTo(view) is False


def test_a_completed_lookup_leaves_the_search_button_usable(qapp) -> None:
    """The retry path has to survive an outcome: a failed or empty lookup is
    exactly when the button is wanted again."""
    service = _Service()
    view = TermsView(_client(service))

    async def scenario() -> None:
        view.query_edit.setText("Jinhsi")
        view._on_search_clicked()
        task = view._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert service.queries == ["Jinhsi"]
    assert view.search_button.isEnabled() is True
    assert view._task is None
