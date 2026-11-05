"""Local-first workbench shell and online configuration flows.

The manuscript owns its file state and canonical actions. Navigation keeps the
existing page indices and keyboard shortcuts; endpoint metadata is local state,
not a claim that a service connection has been tested.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable

from PySide6.QtCore import QSize
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import strings
from ..api import ApiClient
from ..config import ClientConfig, usable_base_url
from ..credentials import CredentialStoreUnavailable, has_token, store_token
from ..errors import (
    ERROR_INSECURE_ENDPOINT,
    ERROR_NOT_CONFIGURED,
    ClientError,
    message_for,
)
from . import error_presentation
from .components import Banner, EmptyStateCard, EndpointChip, fit_to_workspace
from .first_run_dialog import FirstRunDialog
from .icons import icon
from .manuscript_view import ManuscriptView
from .settings_dialog import SettingsDialog
from .status_view import StatusView
from .terms_view import TermsView
from .token_dialog import TokenDialog
from .translate_view import TranslateView

# The navigation column is fixed: it is a landmark, and a landmark that
# resizes with the window stops being one. The content area takes the rest.
NAV_BAR_WIDTH = 60

# What the widest area (the term table's five columns) wants before a
# horizontal scrollbar appears. A PREFERENCE, not a floor: see
# `_fit_to_workspace`.
WINDOW_PREFERRED_MINIMUM = (960, 640)
WINDOW_DEFAULT_SIZE = (1440, 900)

# The smallest useful client size when the workspace permits it. On smaller
# workspaces even this preference must give way to the visible window frame.
WINDOW_ABSOLUTE_MINIMUM = (640, 380)

# The order the owner meets the areas in, and the index each keeps for the
# lifetime of the window: term lookup first because it is the reflex action,
# translation second because it is the one that spends money, and service status
# third. The manuscript workbench follows without changing existing shortcuts.
PAGE_TERMS = 0
PAGE_TRANSLATE = 1
PAGE_STATUS = 2
PAGE_MANUSCRIPT = 3


def endpoint_status_text(base_url: str | None) -> str:
    """The one-sentence description of where this client's requests go.

    A separate function, and the only place the two states are turned into a
    sentence, so the state logic can be exercised without driving the widget.

    An unconfigured client has to LOOK unconfigured. The address used to be
    substituted silently when `config.json` went missing, and the owner then
    read a connection error - about a machine-local development port they had
    never chosen - as the service being down.

    The chip shows a short word and a shortened address because it has to fit
    a 168px column; this sentence is what the window reports to assistive
    technology, so the state is never only available to someone who can read
    an elided string in a small font.
    """
    if base_url is None:
        return strings.ENDPOINT_NOT_CONFIGURED
    return strings.ENDPOINT_CONFIGURED.format(base_url=base_url)


def base_url_refusal(base_url: str | None) -> str | None:
    """Why this address would be refused, or None if it would be accepted.

    The transport is still the authority - `_apply_settings` below keeps its
    own try/except around `update_base_url`, and that is what actually
    protects the running client. This exists so the refusal can be shown
    where the owner can act on it: on the field they typed it into, WHILE the
    dialog is still open, instead of after it has closed over a value the
    client will never use.

    Two checks, in the order the transport makes them, and against the SAME
    predicate `ApiClient` applies (`usable_base_url`) rather than a second
    copy of the confidentiality rule - a pre-flight that can disagree with the
    thing it stands in front of is worse than no pre-flight.
    """
    if base_url is None or not str(base_url).strip():
        return message_for(ERROR_NOT_CONFIGURED)
    if not usable_base_url(base_url):
        return message_for(ERROR_INSECURE_ENDPOINT)
    return None


def _accepts_validator(factory: object) -> bool:
    """Whether `factory` will take a `validator=` keyword.

    The settings dialog is asked to check the address before it closes, which
    it can only do if it has been given something to check with. Passing the
    callback blindly would turn a dialog that does not take one yet - or a
    stand-in a test substitutes - into a TypeError at the moment the owner
    opens Settings, which is precisely the screen they went there to fix.
    """
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return False
    if "validator" in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


class MainWindow(QMainWindow):
    def __init__(
        self, config: ClientConfig | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(strings.APP_TITLE)
        self.config = config if config is not None else ClientConfig.load()
        self.api_client = ApiClient.from_config(self.config)

        # The two credential-shaped actions the dispatch table classifies are
        # handed down here. Both dialogs belong to this window, so a view can
        # only offer them if it is given a way in - and an action the table
        # names but no view can draw is a promise nothing keeps.
        self.translate_view = TranslateView(
            self.api_client,
            self,
            on_open_settings=self._on_settings_clicked,
            on_enter_token=self.enter_token,
        )
        self.terms_view = TermsView(
            self.api_client, self, on_enter_token=self.enter_token
        )
        self.status_view = StatusView(
            self.api_client, self, on_enter_token=self.enter_token
        )
        self.manuscript_view = ManuscriptView(self.api_client, self)

        self.stack = QStackedWidget(self)
        # The pages adapt or scroll internally on a small workspace. Do not
        # let an inactive page impose its full size hint on the whole window.
        self.stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        # Index order is the contract PAGE_* above states; the views are
        # inserted in that order and never reordered afterwards.
        self.stack.addWidget(self.terms_view)
        self.stack.addWidget(self.translate_view)
        self.stack.addWidget(self.status_view)
        self.stack.addWidget(self.manuscript_view)

        self.nav_buttons: list[QPushButton] = []
        self._nav_by_page: dict[int, QPushButton] = {}
        nav_bar = self._build_navigation()

        # Above every area rather than inside one: what it says - no address,
        # or an address that was refused - is true of all three, and while it
        # is showing it is the only thing on screen worth acting on.
        self.global_banner = Banner(self)

        # The same fact as a checklist, for the state where the client cannot
        # send anything at all. Drawn from configuration and the credential
        # store only; it costs no request to show.
        self.setup_card = EmptyStateCard(self)

        content = QWidget(self)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.global_banner)
        content_layout.addWidget(self.setup_card)
        content_layout.addWidget(self.stack, 1)

        footer = QFrame(content)
        footer.setObjectName("workbenchToolbar")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 2, 12, 2)
        self.endpoint_button = QPushButton(footer)
        self.endpoint_button.setObjectName("commandButton")
        self.endpoint_button.clicked.connect(self._on_settings_clicked)
        self.diagnostics_button = QPushButton(strings.WORKBENCH_DIAGNOSTICS, footer)
        self.diagnostics_button.setObjectName("commandButton")
        self.diagnostics_button.clicked.connect(lambda: self.show_page(PAGE_STATUS))
        footer_layout.addWidget(self.endpoint_button)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.diagnostics_button)
        content_layout.addWidget(footer)

        central = QWidget(self)
        central_layout = QHBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(nav_bar)
        central_layout.addWidget(content, 1)
        self.setCentralWidget(central)

        self._build_menu()
        self._build_shortcuts()
        self._connect_translate_bridge()
        lookup_signal = getattr(self.manuscript_view, "lookup_requested", None)
        if lookup_signal is not None:
            lookup_signal.connect(self._on_lookup_requested)
        self.manuscript_view.documentChanged.connect(self._update_window_title)
        self._update_window_title()

        self.show_page(PAGE_MANUSCRIPT)
        self._refresh_endpoint_state()
        self._fit_to_workspace()

    # -- construction ------------------------------------------------------

    def _build_navigation(self) -> QFrame:
        """A narrow activity rail; service status stays in diagnostics."""
        nav_bar = QFrame(self)
        nav_bar.setObjectName("navBar")
        nav_bar.setFixedWidth(NAV_BAR_WIDTH)
        layout = QVBoxLayout(nav_bar)
        layout.setContentsMargins(5, 12, 5, 8)
        layout.setSpacing(4)
        for page, label, name in (
            (PAGE_MANUSCRIPT, strings.NAV_ITEM_MANUSCRIPT, "file-text"),
            (PAGE_TERMS, strings.NAV_ITEM_TERMS, "book-open"),
            (PAGE_TRANSLATE, strings.NAV_ITEM_TRANSLATE, "languages"),
        ):
            button = QPushButton(nav_bar)
            button.setObjectName("activityItem")
            button.setCheckable(True)
            button.setIcon(icon(name))
            button.setIconSize(QSize(22, 22))
            button.setFixedSize(50, 46)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.clicked.connect(lambda _checked=False, target=page: self.show_page(target))
            layout.addWidget(button)
            self._nav_by_page[page] = button
            self.nav_buttons.append(button)
        layout.addStretch(1)
        settings = QPushButton(nav_bar)
        settings.setObjectName("activityItem")
        settings.setIcon(icon("settings"))
        settings.setIconSize(QSize(22, 22))
        settings.setFixedSize(50, 46)
        settings.setToolTip(strings.SETTINGS_TITLE)
        settings.setAccessibleName(strings.SETTINGS_TITLE)
        settings.clicked.connect(self._on_settings_clicked)
        layout.addWidget(settings)
        self.settings_button = settings
        # Keep the full, accessible endpoint state in one existing component.
        # Its compact visual representation lives in the footer instead.
        self.endpoint_chip = EndpointChip(nav_bar)
        self.endpoint_chip.hide()
        return nav_bar

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu(strings.MENU_FILE)
        for text, shortcut, handler in (
            (strings.MANUSCRIPT_NEW, "Ctrl+N", self.manuscript_view.new_document),
            (strings.MANUSCRIPT_OPEN, "Ctrl+O", self.manuscript_view.open_document),
            (strings.MANUSCRIPT_SAVE, "Ctrl+S", self.manuscript_view.save_document),
            (strings.MANUSCRIPT_SAVE_AS, "Ctrl+Shift+S", lambda: self.manuscript_view.save_document(save_as=True)),
        ):
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(
                lambda _checked=False, callback=handler: self._run_manuscript_action(callback)
            )
            file_menu.addAction(action)
        file_menu.addSeparator()
        settings_action = QAction(strings.SETTINGS_MENU_LABEL, self)
        settings_action.triggered.connect(self._on_settings_clicked)
        file_menu.addAction(settings_action)
        status_action = QAction(strings.WORKBENCH_DIAGNOSTICS, self)
        status_action.triggered.connect(lambda: self.show_page(PAGE_STATUS))
        file_menu.addAction(status_action)
        self.status_action = status_action
        quit_action = QAction(strings.MENU_QUIT, self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu(strings.MENU_HELP)
        about_action = QAction(strings.MENU_ABOUT, self)
        about_action.triggered.connect(self._on_about_clicked)
        help_menu.addAction(about_action)

    def _run_manuscript_action(self, handler: Callable[[], object]) -> None:
        # Do not change focus while an IME composition is pending. The
        # canonical handlers repeat the same guard before touching the file.
        if not self.manuscript_view._input_ready():
            if self.stack.currentIndex() != PAGE_MANUSCRIPT:
                self._show_global_banner(
                    strings.MANUSCRIPT_FINISH_INPUT,
                    error_presentation.SEVERITY_WARN,
                )
            return
        self.show_page(PAGE_MANUSCRIPT)
        handler()

    def _build_shortcuts(self) -> None:
        """Reaching an area, and its input, without the mouse.

        Kept together because they are one feature: switching areas from the
        keyboard is pointless if the caret then has to be placed by hand.
        """
        self._shortcuts: list[QShortcut] = []
        for sequence, page in (
            ("Ctrl+1", PAGE_TERMS),
            ("Ctrl+2", PAGE_TRANSLATE),
            ("Ctrl+3", PAGE_STATUS),
            ("Ctrl+4", PAGE_MANUSCRIPT),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(lambda target=page: self.show_page(target))
            self._shortcuts.append(shortcut)
        focus_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        focus_shortcut.activated.connect(self.focus_current_input)
        self._shortcuts.append(focus_shortcut)

    def _connect_translate_bridge(self) -> None:
        """Carry a term-lookup query over to translation, if the lookup area
        offers one.

        The bridge closes the one gap the layout leaves open: a lookup that
        found nothing is exactly the moment the owner wants the model, and
        without this they retype the query in another area. It is connected
        defensively because the signal belongs to the lookup area, and a
        window that refuses to construct because a signal has not been added
        yet takes the whole application with it.
        """
        signal = getattr(self.terms_view, "translate_requested", None)
        connect = getattr(signal, "connect", None)
        if callable(connect):
            connect(self._on_translate_requested)

    # -- navigation --------------------------------------------------------

    def show_page(self, index: int) -> None:
        """Bring one area to the front and mark its navigation item.

        Both halves in one place: a shortcut that changed the stack without
        the button, or a button that changed the button without the stack,
        would leave the column naming an area that is not on screen.
        """
        if not 0 <= index < self.stack.count():
            return
        self.stack.setCurrentIndex(index)
        for page, button in self._nav_by_page.items():
            button.setChecked(page == index)
        # The footer and each online page already state an unconfigured
        # service; preserve the banner space for actual refusals or errors.
        standing_notice = self.global_banner.message_text == strings.GLOBAL_BANNER_NOT_CONFIGURED
        self.global_banner.setVisible(self.global_banner.is_showing() and not standing_notice)

    def _fit_to_workspace(self) -> None:
        """Size the window against the desktop it will actually appear on.

        The minimum used to be a fixed 960x640, described in a comment as
        "no larger than a 1366x768 laptop can show". That is true at 100%
        scaling and false at every other setting - see `fit_to_workspace`,
        which holds the clamp itself. It lives in components.py because the
        settings dialog needs the same treatment, and a second copy would
        have been a second thing to forget.
        """
        fit_to_workspace(
            self,
            WINDOW_PREFERRED_MINIMUM,
            WINDOW_DEFAULT_SIZE,
            WINDOW_ABSOLUTE_MINIMUM,
        )

    def focus_current_input(self) -> None:
        """Put the caret in whatever the current area's main input is.

        The area knows which of its widgets that is; this asks it and falls
        back to focusing the area itself, so an area that has not published a
        `focus_input` still ends up somewhere the keyboard can work.
        """
        page = self.stack.currentWidget()
        if page is None:
            return
        focuser = getattr(page, "focus_input", None)
        if callable(focuser):
            focuser()
            return
        page.setFocus()

    def _update_window_title(self) -> None:
        title = self.manuscript_view.document_title
        dirty = " *" if self.manuscript_view.document_dirty else ""
        self.setWindowTitle(f"{title}{dirty} — {strings.APP_TITLE}")

    def _on_lookup_requested(self, query: str) -> None:
        self.show_page(PAGE_TERMS)
        self.terms_view.query_edit.setText(query)
        self.terms_view.query_edit.setFocus()

    def _on_translate_requested(self, query: str) -> None:
        self.show_page(PAGE_TRANSLATE)
        prefill = getattr(self.translate_view, "prefill", None)
        if callable(prefill):
            prefill(query)
            return
        editor = getattr(self.translate_view, "input_edit", None)
        if editor is not None:
            editor.setPlainText(query)
            editor.setFocus()

    # -- endpoint state ----------------------------------------------------

    def _refresh_endpoint_state(self) -> None:
        """Restate the address everywhere it is claimed, from one reading.

        The chip, the global banner and the checklist are three views of a
        single fact. Refreshing them together is what keeps them from
        disagreeing - a window that says "not configured" in one corner and
        shows an address in another is worse than one that says nothing.
        """
        self._refresh_endpoint_chip()
        self._refresh_global_banner()
        self._refresh_setup_card()

    def _refresh_endpoint_chip(self) -> None:
        base_url = self.config.base_url
        configured = base_url is not None
        if configured:
            state_text = strings.ENDPOINT_CHIP_CONFIGURED
            address_text = base_url
            tooltip = strings.ENDPOINT_CHIP_TOOLTIP_CONFIGURED.format(
                base_url=base_url
            )
        else:
            state_text = strings.ENDPOINT_CHIP_NOT_CONFIGURED
            address_text = strings.ENDPOINT_CHIP_NO_ADDRESS
            tooltip = strings.ENDPOINT_CHIP_TOOLTIP_NOT_CONFIGURED
        self.endpoint_chip.set_state(configured, state_text, address_text, tooltip)
        # The chip is short by necessity; the full sentence is what a screen
        # reader gets, so the state is not only available to someone who can
        # read an elided address.
        self.endpoint_chip.setAccessibleName(endpoint_status_text(base_url))
        self.endpoint_button.setText(
            strings.WORKBENCH_ENDPOINT_CONFIGURED if configured
            else strings.WORKBENCH_ENDPOINT_UNCONFIGURED
        )
        self.endpoint_button.setToolTip(endpoint_status_text(base_url))
        self.endpoint_button.setAccessibleName(endpoint_status_text(base_url))

    def _refresh_global_banner(self) -> None:
        """Show the standing "nothing will be sent" notice, or take it down.

        Not closable: it does not report an event that has passed, it reports
        the condition the client is in, and dismissing it would leave a
        window that sends nothing and does not say so.
        """
        if self.config.base_url is None:
            self._show_global_banner(
                strings.GLOBAL_BANNER_NOT_CONFIGURED,
                error_presentation.SEVERITY_WARN,
                action=error_presentation.ACTION_OPEN_SETTINGS,
                closable=False,
            )
            self.global_banner.hide()
            return
        self.global_banner.clear()

    def _refresh_setup_card(self) -> None:
        # Online pages expose configuration in their own empty states; local
        # work must never be displaced by a setup checklist.
        self.setup_card.setVisible(False)

    def _show_global_banner(
        self,
        text: str,
        severity: str,
        action: str | None = None,
        closable: bool = True,
    ) -> None:
        actions: tuple[tuple[str, Callable[[], None]], ...] = ()
        if action is not None:
            label = error_presentation.ACTION_LABEL_BY_ACTION.get(action)
            handler = self._handler_for_action(action)
            if label is not None and handler is not None:
                actions = ((label, handler),)
        self.global_banner.show_message(
            text, severity, actions=actions, closable=closable
        )

    def _handler_for_action(self, action: str) -> Callable[[], None] | None:
        if action == error_presentation.ACTION_OPEN_SETTINGS:
            return self._on_settings_clicked
        if action == error_presentation.ACTION_ENTER_TOKEN:
            return self.enter_token
        return None

    def enter_token(self) -> None:
        """Replace the stored credential, from wherever the refusal appeared.

        The dispatch table has always classified `unauthorized` and
        `forbidden` as "enter a new token", but no view could act on it: this
        window owns the dialog and the credential store, and the views were
        left dropping the classified action on the floor. So the table
        promised a button that nothing drew.

        Reached from an area's banner, so it goes straight to the token
        dialog rather than to Settings: the thing to fix is one field, and
        routing through a settings dialog to find it is the extra hop this
        classification exists to remove.
        """
        dialog = TokenDialog(self)
        while True:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            token = dialog.token()
            if not token:
                return
            try:
                store_token(token)
            except CredentialStoreUnavailable:
                # Re-shown with the token still in it, rather than reporting
                # the failure somewhere else and dropping what was typed.
                # The credential store being briefly unavailable is the one
                # failure here that retrying actually fixes, and making the
                # owner paste a long secret again to retry is the opposite of
                # what the dialog's own show_store_error was built for.
                dialog.show_store_error(strings.CREDENTIAL_STORE_ERROR_MESSAGE)
                continue
            break
        # The status area reports whether a credential is stored; it read that
        # when it was built, which was before this.
        self.status_view.refresh_credential_state()

    def _show_refusal(self, exc: ClientError) -> None:
        """Report an address the transport would not take.

        On the global banner because that is where the dispatch table puts
        both address refusals, and because the refusal is true of every area
        rather than of one request. It stays until the address is settled
        while the client has none - closing it would hide the only remaining
        statement that nothing is being sent - and is dismissable once there
        is a working address to fall back on.
        """
        presentation = error_presentation.presentation_for(exc.code)
        self._show_global_banner(
            exc.message,
            presentation.severity,
            action=presentation.action,
            closable=self.config.base_url is not None,
        )

    def _discard_previous_endpoint_state(self) -> None:
        """Nothing on screen may outlive the address it came from.

        Changing the server address does not stop a request already on its
        way to the old one, and a reply carries no marking that would let a
        view know it is stale. Without this, an answer from the PREVIOUS
        service renders under the new address in the chip - the most
        misleading thing this window can display, and the same class of
        defect as the silent fallback this whole change exists to remove.

        Each view cancels its own in-flight task through the mechanism it
        already had, and clears the state that belonged to that endpoint.
        Nothing here coordinates the cancellations or waits for them: they
        are fire-and-forget, exactly as the Cancel button's are.
        """
        for view in (self.translate_view, self.terms_view, self.status_view, self.manuscript_view):
            view.reset_for_endpoint_change()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self.manuscript_view.confirm_discard():
            event.ignore()
            return
        self.manuscript_view._cancel_pending()
        super().closeEvent(event)

    # -- settings ----------------------------------------------------------

    def _on_settings_clicked(self) -> None:
        dialog = self._new_settings_dialog()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_settings(dialog.result_config())
        # Whether or not the dialog was accepted: entering, changing or
        # forgetting a token inside it writes to the credential store
        # immediately, and Cancel does not undo that.
        self.status_view.refresh_credential_state()
        self._refresh_setup_card()

    def _new_settings_dialog(self) -> QDialog:
        """Hand the dialog the address check, so it can refuse before closing.

        This is the whole of the control-flow change behind the inlined
        refusal: the check used to run after `exec()` returned, by which time
        the dialog - and the field the owner would have to correct - was
        already gone.
        """
        if _accepts_validator(SettingsDialog):
            dialog = SettingsDialog(self.config, self, validator=base_url_refusal)
        else:
            dialog = SettingsDialog(self.config, self)
        signal = getattr(dialog, "diagnosticsRequested", None)
        if signal is not None:
            signal.connect(lambda: self.show_page(PAGE_STATUS))
        return dialog

    def _apply_settings(self, new_config: ClientConfig) -> None:
        """Adopt an accepted settings change, or refuse it whole.

        The address goes to the live transport FIRST, because it is the only
        part the client can refuse: an address that would carry the device
        token in the clear is rejected there. Adopting such a configuration -
        or writing it to disk, where it would come back at the next launch -
        would leave settings that do not describe the running client, so the
        refusal keeps the previous address in effect and says why.

        Zero half-application is the invariant, and the ordering is what
        enforces it: on the refusal path `self.config`, the timeouts, the
        in-flight requests and the file on disk are all still untouched,
        because every one of them is written after the raising call.
        """
        previous_base_url = self.config.base_url
        if new_config.base_url != previous_base_url:
            try:
                self.api_client.update_base_url(new_config.base_url)
            except ClientError as exc:
                self._show_refusal(exc)
                return
        self.config = new_config
        # Timeouts too, or the saved settings and the running client
        # disagree until the next launch.
        self.api_client.update_timeouts(
            self.config.request_timeout_seconds,
            self.config.translate_timeout_seconds,
        )
        address_changed = new_config.base_url != previous_base_url
        if address_changed:
            self._discard_previous_endpoint_state()
        # ...and the address on screen, which is the whole point of showing
        # it: a client that has just been configured must stop saying it is
        # not. This also takes down any refusal banner the last attempt left.
        self._refresh_endpoint_state()
        if address_changed:
            # Three areas going empty at once reads as data loss unless
            # something says it was deliberate. Informational, and closable:
            # it reports something that has already finished happening.
            self.global_banner.show_message(
                strings.ENDPOINT_CHANGED_BANNER,
                error_presentation.SEVERITY_INFO,
            )
            self.endpoint_chip.flash()
        try:
            self.config.save()
        except OSError:
            # The settings still take effect for this session; what the
            # owner must not be left believing is that they were kept. It
            # replaces the address-changed notice above because it is the
            # more consequential of the two, and the banner holds one
            # message at a time by design.
            self.global_banner.show_message(
                strings.SETTINGS_NOT_SAVED_MESSAGE,
                error_presentation.SEVERITY_WARN,
            )

    def _on_about_clicked(self) -> None:
        # Built rather than called through the static helper, so its one
        # button carries this application's wording instead of the English
        # Qt supplies for a translation that is not installed.
        about = QMessageBox(self)
        about.setWindowTitle(strings.MENU_ABOUT)
        about.setText(strings.ABOUT_TEXT)
        about.setIcon(QMessageBox.Icon.Information)
        about.setStandardButtons(QMessageBox.StandardButton.Ok)
        about.button(QMessageBox.StandardButton.Ok).setText(strings.DIALOG_OK_BUTTON)
        about.exec()

    # -- first run ---------------------------------------------------------

    def ensure_credential(self) -> bool:
        """Run the first-run flow if no device credential is stored yet.

        Returns True once a credential is present (already stored, or just
        entered); False if the user chose to quit instead.

        A credential store that cannot be written is NOT a reason to return
        True: the whole point of the return value is that no window opens
        without a credential. It is also not a reason to return False, which
        would close the application over a vault that is momentarily
        unavailable - so the failure is reported inside the dialog and the
        dialog is offered again. Only `reject` - the Quit button - ends this.
        """
        if has_token():
            return True
        dialog = FirstRunDialog(self)
        while dialog.exec() == QDialog.DialogCode.Accepted:
            token = dialog.token()
            if not token:
                # The dialog refuses an empty token before it accepts, so
                # this is a dialog that is not behaving like one; retrying it
                # would spin rather than get anywhere.
                return False
            try:
                store_token(token)
            except CredentialStoreUnavailable:
                reporter = getattr(dialog, "show_store_error", None)
                if not callable(reporter):
                    # Nowhere to put the failure means nothing for the owner
                    # to retry against; refusing entry is the safe end of
                    # this branch, and it is the behaviour that predates the
                    # inline report.
                    return False
                reporter(strings.CREDENTIAL_STORE_ERROR_MESSAGE)
                continue
            # The status view read the credential state when it was built,
            # which was before this. Without the refresh it keeps reporting a
            # missing credential for the whole session - and the setup
            # checklist would keep showing step two unfinished.
            self.status_view.refresh_credential_state()
            self._refresh_setup_card()
            return True
        return False
