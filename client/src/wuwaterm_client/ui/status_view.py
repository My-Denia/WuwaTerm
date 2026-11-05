"""Service/meta status: version, data profile/commit, term count, whether a
translation model is configured, and the active credential-store backend.

Two cards rather than one form. The upper card describes the SERVICE the
client is pointed at and is discarded whenever the address changes; the lower
one describes THIS MACHINE's credential store and survives that change. They
were one undifferentiated list of rows before, which is exactly the reading a
reset has to disprove - an owner checking which service they are talking to
had no way to see that half the rows had just been invalidated and half had
not.
"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import strings
from ..api import SUPPORTED_API_VERSIONS, ApiClient, MetaResult
from ..credentials import CredentialStoreUnavailable, active_backend_name, read_token
from ..errors import ClientError
from . import error_presentation
from .components import (
    Banner,
    EmptyStateCard,
    ProgressLine,
    StatusStrip,
    apply_credential_backend,
)


def _card(title: str, parent: QWidget) -> tuple[QFrame, QVBoxLayout]:
    """A titled surface, and the layout its content goes in.

    QFrame, not QWidget: a bare QWidget draws no background or border from a
    stylesheet, so the same rules would produce an invisible card. The margins
    are zeroed because the surface's own padding comes from the stylesheet -
    setting both would indent the content twice at 150% scaling.
    """
    frame = QFrame(parent)
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    caption = QLabel(title, frame)
    caption.setObjectName("cardTitle")
    layout.addWidget(caption)
    return frame, layout


class StatusView(QWidget):
    def __init__(
        self,
        api_client: ApiClient,
        parent: QWidget | None = None,
        *,
        on_enter_token: "object | None" = None,
    ) -> None:
        super().__init__(parent)
        # See TermsView: the credential dialog belongs to the window, so the
        # "enter a new token" action the dispatch table assigns to a rejected
        # credential can only be offered if the window hands down a way in.
        self._on_enter_token = on_enter_token
        self._api_client = api_client
        self._task: asyncio.Task | None = None
        # Every refresh carries the generation it began in, and writes nothing
        # once that generation is stale. This is not belt-and-braces on the
        # duplicate-click guard below: api.py CONSUMES asyncio.CancelledError
        # and raises ClientError(cancelled) in its place, so a refresh
        # cancelled by an address change still runs its own rendering code and
        # would repaint - with the PREVIOUS server's failure - an area that was
        # just cleared for the new address.
        self._generation = 0
        # Whether the rows below describe a service this client actually asked.
        # Untouched rows read as facts about the current address otherwise.
        self._has_data = False
        # Set by an address change, so the empty state can say "switched"
        # rather than "nothing fetched yet" - the second reads as data loss.
        self._endpoint_changed = False
        # Whether the banner currently carries THIS view's api-version
        # warning. Tracked so an accepted reply takes down that warning and
        # only that warning: `_show_meta` must not become a second place where
        # an unrelated message is silently swept off the screen.
        self._api_version_warned = False

        self.service_version_value = QLabel(strings.STATUS_UNKNOWN_VALUE, self)
        self.data_profile_value = QLabel(strings.STATUS_UNKNOWN_VALUE, self)
        # Identifiers, not prose: a commit and a count are compared character
        # by character against something the operator quotes, and a
        # proportional font makes that comparison harder than it needs to be.
        self.data_commit_value = QLabel(strings.STATUS_UNKNOWN_VALUE, self)
        self.data_commit_value.setObjectName("monoLabel")
        self.term_count_value = QLabel(strings.STATUS_UNKNOWN_VALUE, self)
        self.term_count_value.setObjectName("monoLabel")
        self.model_configured_value = QLabel(strings.STATUS_UNKNOWN_VALUE, self)
        self.keyring_backend_value = QLabel(self)
        self.credential_status_value = QLabel(self)

        self.refresh_button = QPushButton(strings.STATUS_REFRESH_BUTTON, self)
        self.refresh_button.setObjectName("primaryButton")
        self.refresh_button.clicked.connect(self._on_refresh_clicked)

        # A hairline, never a modal: this view stays readable - and its
        # credential rows stay true - while a request is in flight.
        self._progress = ProgressLine(self)
        self._banner = Banner(self)
        self._empty_card = EmptyStateCard(self)
        self.status_label = StatusStrip(self)

        service_card, service_layout = _card(strings.STATUS_TAB_TITLE, self)
        self._service_card = service_card
        service_form = QFormLayout()
        service_form.setContentsMargins(0, 0, 0, 0)
        service_form.setHorizontalSpacing(16)
        service_form.setVerticalSpacing(8)
        service_form.addRow(
            strings.STATUS_SERVICE_VERSION_LABEL, self.service_version_value
        )
        service_form.addRow(strings.STATUS_DATA_PROFILE_LABEL, self.data_profile_value)
        service_form.addRow(strings.STATUS_DATA_COMMIT_LABEL, self.data_commit_value)
        service_form.addRow(strings.STATUS_TERM_COUNT_LABEL, self.term_count_value)
        service_form.addRow(
            strings.STATUS_MODEL_CONFIGURED_LABEL, self.model_configured_value
        )
        service_layout.addLayout(service_form)

        credential_card, credential_layout = _card(
            strings.SETTINGS_CREDENTIAL_SECTION_TITLE, self
        )
        self._credential_card = credential_card
        credential_form = QFormLayout()
        credential_form.setContentsMargins(0, 0, 0, 0)
        credential_form.setHorizontalSpacing(16)
        credential_form.setVerticalSpacing(8)
        credential_form.addRow(
            strings.STATUS_KEYRING_BACKEND_LABEL, self.keyring_backend_value
        )
        credential_form.addRow(
            strings.SETTINGS_CREDENTIAL_SECTION_TITLE, self.credential_status_value
        )
        credential_layout.addLayout(credential_form)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        action_row.addStretch(1)
        action_row.addWidget(self.refresh_button)

        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 12, 12, 12)
        body_layout.setSpacing(12)
        body_layout.addWidget(self._progress)
        body_layout.addWidget(self._banner)
        body_layout.addLayout(action_row)
        body_layout.addWidget(self._empty_card)
        body_layout.addWidget(self._service_card)
        body_layout.addWidget(self._credential_card)
        body_layout.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.status_label)
        self.scroll_area = scroll

        self._refresh_credential_labels()
        self._apply_empty_state()
        self._apply_endpoint_state()

    # -- public API --------------------------------------------------------

    def focus_input(self) -> None:
        """This area has no text input, so the caret goes to its one action.

        Returning nothing focusable would leave Ctrl+K silently doing nothing
        in one of the three areas, which is worse than focusing the button:
        the shortcut has to mean the same thing everywhere.
        """
        self.refresh_button.setFocus()

    def refresh_credential_state(self) -> None:
        """Public: the credential can change outside this view (first run,
        Settings), and the labels are only read when something asks."""
        self._refresh_credential_labels()
        # The setup checklist marks the token step from the credential store,
        # so a credential entered elsewhere has to re-mark it here too.
        self._apply_empty_state()

    def reset_for_endpoint_change(self) -> None:
        """Forget the metadata the previous server address reported.

        Version, data profile, data commit and term count identify a
        SERVICE. Left on screen after the address changes they describe the
        wrong one, and this is the area an owner reads precisely to find out
        which service they are talking to.

        The credential-store card is deliberately untouched: it describes
        this machine, not the server, and is the same whichever address is
        configured.

        Bumping the generation is part of the clearing, not an extra: the
        cancelled request below still runs to completion inside api.py and
        arrives back here as an ordinary error, and without the bump it would
        write the old server's failure over the state this method just reset.
        """
        self._generation += 1
        if self._task is not None:
            self._task.cancel()
        self._task = None
        for value_label in (
            self.service_version_value,
            self.data_profile_value,
            self.data_commit_value,
            self.term_count_value,
            self.model_configured_value,
        ):
            value_label.setText(strings.STATUS_UNKNOWN_VALUE)
        self.status_label.clear()
        self._banner.clear()
        self._api_version_warned = False
        self._progress.stop()
        self._has_data = False
        self._endpoint_changed = True
        self._apply_empty_state()
        self._apply_endpoint_state()

    # -- internals ---------------------------------------------------------

    def _refresh_credential_labels(self) -> None:
        try:
            backend = active_backend_name()
        except Exception:
            # Keyring backend discovery itself can fail before a read is
            # attempted; it must not prevent the local editor from opening.
            self.keyring_backend_value.setText(strings.STATUS_UNKNOWN_VALUE)
            self.keyring_backend_value.setToolTip("")
        else:
            apply_credential_backend(self.keyring_backend_value, backend)
        try:
            stored = read_token() is not None
        except CredentialStoreUnavailable:
            credential_status = strings.SETTINGS_TOKEN_STATUS_UNKNOWN
        else:
            credential_status = (
                strings.SETTINGS_TOKEN_STATUS_STORED
                if stored else strings.SETTINGS_TOKEN_STATUS_MISSING
            )
        self.credential_status_value.setText(credential_status)

    def _apply_endpoint_state(self) -> None:
        """Refuse to offer a request this client would refuse to send.

        The transport still answers `not_configured` underneath - that is the
        guarantee. Disabling the button is noise reduction on top of it, so
        the owner is not invited to press something whose only outcome is an
        error they have already been told about above.
        """
        configured = self._api_client.is_configured
        idle = self._task is None or self._task.done()
        self.refresh_button.setEnabled(configured and idle)
        self.refresh_button.setToolTip(
            "" if configured else strings.TOOLTIP_NEEDS_ENDPOINT
        )

    def _apply_empty_state(self) -> None:
        """Whichever of the three empty states is true, and never the card and
        the data at once."""
        if self._has_data:
            self._empty_card.setVisible(False)
            self._service_card.setVisible(True)
            return
        if not self._api_client.is_configured:
            # This state is local and must not trigger a service request.
            self._empty_card.set_content(
                strings.EMPTY_STATUS_UNCONFIGURED_TITLE,
                strings.EMPTY_UNCONFIGURED_SUBTITLE,
            )
        elif self._endpoint_changed:
            self._empty_card.set_content(
                strings.ENDPOINT_CHANGED_TITLE,
                strings.ENDPOINT_CHANGED_SUBTITLE,
                (strings.STATUS_REFRESH_BUTTON, self._on_refresh_clicked),
            )
        else:
            self._empty_card.set_content(
                strings.EMPTY_STATUS_TITLE,
                strings.EMPTY_STATUS_SUBTITLE,
                (strings.STATUS_REFRESH_BUTTON, self._on_refresh_clicked),
            )
        self._empty_card.setVisible(True)
        self._service_card.setVisible(False)

    def _on_refresh_clicked(self) -> None:
        # The button is disabled inside the coroutine, which does not run
        # until the loop gets a turn. Two activations before that start two
        # refreshes whose replies can land in either order.
        if self._task is not None and not self._task.done():
            return
        if not self._api_client.is_configured:
            return
        self._generation += 1
        self._task = asyncio.ensure_future(self._run_refresh(self._generation))

    async def _run_refresh(self, generation: int) -> None:
        self.refresh_button.setEnabled(False)
        self._banner.clear()
        self._progress.start()
        self.status_label.set_text(strings.STATUS_LOADING)
        try:
            meta = await self._api_client.get_meta()
        except ClientError as exc:
            # Every exit compares generations BEFORE touching the screen. A
            # refresh whose address has been replaced has nothing true left to
            # say, including about its own failure.
            if generation != self._generation:
                return
            self._show_error(exc)
        else:
            if generation != self._generation:
                return
            self._show_meta(meta)
            self.status_label.set_text(strings.STATUS_BAR_DONE)
        finally:
            if generation == self._generation:
                self._progress.stop()
                self._task = None
                self._refresh_credential_labels()
                self._apply_empty_state()
                self._apply_endpoint_state()

    def _show_error(self, exc: ClientError) -> None:
        """Route a failure to the surface the shared table assigns it.

        The table is the same one every other area reads, so a code cannot be
        loud here and silent elsewhere. Only the retry action is offered from
        this area: opening Settings and entering a token are the window's to
        perform, and a button that cannot do what it says is worse than the
        message alone, which already names where to go.
        """
        presentation = error_presentation.presentation_for(exc.code)
        if presentation.surface == error_presentation.SURFACE_STATUS:
            self.status_label.set_text(presentation.message)
            return
        actions: tuple = ()
        label = presentation.action_label
        if presentation.action == error_presentation.ACTION_RETRY and label:
            actions = ((label, self._on_refresh_clicked),)
        elif (
            presentation.action == error_presentation.ACTION_ENTER_TOKEN
            and label
            and self._on_enter_token is not None
        ):
            actions = ((label, self._on_enter_token),)
        # WITH the id, unlike the translate and lookup areas: those two render
        # it in a row of their own, so a copy here would be the second one on
        # screen. This area has no such row - a status refresh has no result
        # block to hang one under - so the banner is the only place the id can
        # be, and dropping it would leave the one failure an operator is most
        # likely to be asked about without a handle.
        self._banner.show_message(
            presentation.message,
            presentation.severity,
            actions=actions,
            request_id=exc.request_id,
        )
        self.status_label.set_text(strings.STATUS_BAR_LAST_REQUEST_FAILED)

    def _show_meta(self, meta: MetaResult) -> None:
        self.service_version_value.setText(meta.service_version)
        profile = meta.source_profile or strings.STATUS_UNKNOWN_VALUE
        commit = meta.source_commit or strings.STATUS_UNKNOWN_VALUE
        model_configured = strings.STATUS_YES if meta.llm_configured else strings.STATUS_NO
        self.data_profile_value.setText(profile)
        self.data_commit_value.setText(commit)
        self.term_count_value.setText(str(meta.term_count))
        self.model_configured_value.setText(model_configured)
        self._has_data = True
        self._endpoint_changed = False
        self._apply_api_version(meta)

    def _apply_api_version(self, meta: MetaResult) -> None:
        """Say so when the service speaks an API version this client does not.

        A WARNING, deliberately, and never a refusal. The rows above are
        written first and stay written: the owner needs to SEE the version
        that is wrong, on the one screen that exists to tell them which
        service they are talking to, and hiding the facts behind an error
        would take that away at exactly the moment it is needed. Nothing here
        disables a request path either - this client keeps working against a
        service it may not fully agree with, because a field it cannot use is
        a smaller problem than a client that will not talk at all.

        It costs no request. The reply examined here is the one the refresh
        above already fetched; the check adds no call, no header and no
        startup traffic (issues #68 / #80), so an unconfigured client still
        sends nothing whatsoever.

        Ordering matters and is why the message lands in the banner from
        HERE: `_run_refresh` clears the banner BEFORE the request, so a
        message written after the reply survives to be read - and the
        `finally` below it touches the progress line, the credential rows and
        the empty state, none of which write the banner.
        """
        if meta.api_version in SUPPORTED_API_VERSIONS:
            # A good refresh after a bad one must not leave the old warning
            # standing. In the refresh path the pre-request clear has already
            # done it; `_show_meta` is also reachable directly, and a warning
            # that outlives the condition it describes is worse than none.
            if self._api_version_warned:
                self._banner.clear()
                self._api_version_warned = False
            return
        self._banner.show_message(
            strings.STATUS_API_VERSION_UNSUPPORTED.format(
                reported=meta.api_version,
                supported=strings.LIST_SEPARATOR.join(SUPPORTED_API_VERSIONS),
            ),
            error_presentation.SEVERITY_WARN,
            # This IS about a request, and this area has no result block to
            # hang the id under - the same reason `_show_error` passes it.
            request_id=meta.request_id,
        )
        self._api_version_warned = True
