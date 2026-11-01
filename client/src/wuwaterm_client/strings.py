"""Every user-facing literal in the WuwaTerm desktop client, in one place.

The ui/ package and errors.py import display-text constants from this module.
Text provided by Qt or returned by the service is handled separately.

The values are Chinese; the constant NAMES are a contract. Tests and the
error map identify a message by its name, so a name may never be renamed or
dropped to change wording - only its value moves.

The flat list makes the application's own wording easy to find. Mappings from
service values to text live in the views that consume them.

Two wordings here are not free choices:

* the cancellation lines must match what client/README.md pins down in
  English - cancelling ends the WAIT, not the work. A request the service
  already holds is finished and paid for whether or not anyone is still
  listening, so nothing here may promise that the request was stopped.
* the endpoint chip says "configured", never "connected". Nothing in this
  client has spoken to the server before its first successful request, so
  the stronger word would be a claim it cannot back.
"""

from __future__ import annotations

# -- Application chrome ----------------------------------------------------
# The product name stays as it is: it names the executable, the window in the
# taskbar and the service the operator hands out credentials for. It is an
# identifier the owner matches against, not a sentence to be read, and the
# design wireframes carry it untranslated for the same reason.

APP_TITLE = "WuwaTerm"

MENU_FILE = "文件(&F)"
MENU_QUIT = "退出"
MENU_HELP = "帮助(&H)"
MENU_ABOUT = "关于"
ABOUT_TEXT = "WuwaTerm 桌面客户端。调用 wuwaterm 的 HTTP 接口，并展示接口返回的结果。"

STATUS_BAR_READY = "就绪"
STATUS_BAR_TRANSLATING = "正在翻译…"
STATUS_BAR_SEARCHING = "搜索中…"
STATUS_BAR_DONE = "完成"
STATUS_BAR_LAST_REQUEST_FAILED = "上次请求失败"
STATUS_BAR_NOT_CONFIGURED = "未配置"
STATUS_COPIED = "已复制到剪贴板。"

# -- Connection state (main window) ----------------------------------------
# Shown at all times: which server this client talks to, or that it has none.
# The unconfigured line names where to fix it, because the state it describes
# is reached without the owner doing anything - a missing or unreadable
# config.json puts the client here on the next launch. It must not offer a
# development address as an example: that is the substitution this whole
# state exists to stop being silent about.

ENDPOINT_CONFIGURED = "服务器地址：{base_url}"
ENDPOINT_NOT_CONFIGURED = (
    "服务器地址：尚未配置。请先在「文件 > 设置」中填写服务器地址，再发起请求。"
)

ENDPOINT_CHIP_CONFIGURED = "已配置"
ENDPOINT_CHIP_NOT_CONFIGURED = "未配置"
ENDPOINT_CHIP_NO_ADDRESS = "未设置地址"
ENDPOINT_CHIP_TOOLTIP_CONFIGURED = "本客户端的请求都会发往：{base_url}"
ENDPOINT_CHIP_TOOLTIP_NOT_CONFIGURED = (
    "尚未配置服务器地址，不会发出任何请求。可在「文件 > 设置」中填写。"
)

STATUS_UNKNOWN_VALUE = "未知"
STATUS_YES = "是"
STATUS_NO = "否"
STATUS_LOADING = "加载中…"

# -- Navigation and page titles --------------------------------------------
# The order the owner meets them in: term lookup first, because it is the
# reflex action; translation second, because it is the one that spends money;
# service status last, because it is only read when something looks wrong.

NAV_ITEM_TERMS = "术语查词"
NAV_ITEM_TRANSLATE = "文本翻译"
NAV_ITEM_STATUS = "服务状态"

PAGE_TITLE_TERMS = "术语查词"
PAGE_TITLE_TRANSLATE = "文本翻译"
PAGE_TITLE_STATUS = "服务状态"

# -- Direction selector ------------------------------------------------

DIRECTION_AUTO = "自动"
DIRECTION_TO_EN = "中译英"
DIRECTION_TO_ZH = "英译中"

# -- Translate tab -----------------------------------------------------

TRANSLATE_TAB_TITLE = "文本翻译"
INPUT_LABEL = "原文"
INPUT_PLACEHOLDER = "输入要翻译的文本…"
DIRECTION_LABEL = "方向"
TRANSLATE_BUTTON = "翻译"
CANCEL_BUTTON = "取消"
RESULT_LABEL = "译文"
RESULT_PLACEHOLDER = "翻译结果会显示在这里。"

KIND_LABEL_EXACT = "词典精确匹配"
KIND_LABEL_FUZZY = "词典模糊匹配"
KIND_LABEL_LLM = "模型翻译"
KIND_LABEL_NOOP = "无需翻译"

DICTIONARY_MISS_NOTE = "未匹配到官方术语，该结果不具权威性。"
DICTIONARY_MISS_BADGE = "未匹配官方术语"
REQUEST_ID_LABEL = "请求 ID：{request_id}"
REQUEST_ID_ROW_LABEL = "请求 ID"
REQUEST_ID_PLACEHOLDER = "—"
REQUEST_ID_COPY_BUTTON = "复制"

# -- Cancellation ----------------------------------------------------------
# client/README.md pins the semantics in English: cancelling ends this
# client's wait. Once the service holds the whole request it finishes it,
# records it as an ordinary completed request, and bills whatever the model
# call cost. A dictionary hit never reaches the model and costs nothing
# either way. So the wording below stops at "stopped waiting", and the note
# says the rest out loud instead of leaving the owner to assume a refund.

STATUS_CANCELLING = "正在取消…"
STATUS_CANCELLED = "已取消等待。"
STATUS_CANCELLED_NOTE = (
    "取消的是本机的等待：服务端可能仍在处理这次请求，已经产生的开销不会退回。"
)

# -- Term lookup tab -----------------------------------------------------

TERMS_TAB_TITLE = "术语查词"
TERMS_QUERY_LABEL = "查询词"
TERMS_QUERY_PLACEHOLDER = "输入中文 / 英文 / 拼音，回车或点「搜索」"
TERMS_SEARCH_BUTTON = "搜索"
TERMS_COLUMN_ZH = "中文"
TERMS_COLUMN_EN = "英文"
TERMS_COLUMN_CATEGORY = "分类"
TERMS_COLUMN_SCORE = "匹配度"
TERMS_COLUMN_REASON = "匹配方式"
TERMS_EMPTY = "没有找到匹配的术语。"
TERMS_SEARCHING = "正在搜索术语…"

# The fourth trigger gate: a query long enough or broken enough to be a
# sentence is not sent to the term endpoint at all, and the panel says where
# such text belongs instead of returning an empty table.
TERMS_SENTENCE_HINT_TITLE = "这更像是一句话，而不是一个术语"
TERMS_SENTENCE_HINT_SUBTITLE = "查词只处理短术语。整句请改用文本翻译区。"
TERMS_TOO_SHORT_HINT = "再多输入一个字就会开始查询。"
TERMS_TRANSLATE_BRIDGE_BUTTON = "用模型翻译「{query}」"

# The match-reason vocabulary, shown as a shape plus a word so the reason is
# readable in a grayscale screenshot and by a color-blind reader. The service
# currently emits exact / fuzzy / low-score; the pinyin words cover the
# remaining documented reasons without a second release.
REASON_LABEL_EXACT = "精确"
REASON_LABEL_PINYIN_FULL = "拼音全拼"
REASON_LABEL_PINYIN_INITIALS = "拼音缩写"
REASON_LABEL_PINYIN_PREFIX = "拼音前缀"
REASON_LABEL_PINYIN_CONTAINS = "拼音包含"
REASON_LABEL_FUZZY = "模糊"
REASON_LABEL_LOW_SCORE = "低相关"

# -- Status tab -----------------------------------------------------

STATUS_TAB_TITLE = "服务状态"
STATUS_SERVICE_VERSION_LABEL = "服务版本"
STATUS_DATA_PROFILE_LABEL = "数据档位"
STATUS_DATA_COMMIT_LABEL = "数据提交号"
STATUS_TERM_COUNT_LABEL = "术语条数"
STATUS_MODEL_CONFIGURED_LABEL = "已配置翻译模型"
STATUS_REFRESH_BUTTON = "刷新"
STATUS_KEYRING_BACKEND_LABEL = "凭据存储后端"

# 接口版本对不上时的那一句。两个版本号都必须出现在文字里:只说「不兼容」的提示,
# 读的人既不知道对面是哪一版,也不知道自己这一版要的是哪一版,拿着它什么也做不了。
# 取值由 api.SUPPORTED_API_VERSIONS 与服务端 /v1/meta 的 api_version 填入 ——
# 与本模块其余「服务端取值 → 文案」的做法一致:文案在这里,取哪个值在消费它的视图里。
# 措辞停在「可能对不上」:这是一条警告,不是拒绝,客户端并不会因此停止工作,所以这里
# 不能写成一句「无法使用」。
STATUS_API_VERSION_UNSUPPORTED = (
    "服务端报告的接口版本是 {reported}，这个客户端支持的是 {supported}。"
    "下面的服务信息仍然照常显示，但两边不是同一个接口版本时，某些字段或功能可能对不上。"
    "请把客户端或服务端升级到相互匹配的版本。"
)

# 把若干取值排成一行时用的顿号。它只有一个字,但它是**显示出来的**一个字,而且是
# 中文标点 —— 换成英文逗号或斜杠,那一句读起来就不是中文了。放在这里的直接理由是
# ui/ 里不允许出现含中日韩字符的字面量(见本模块开头的第三条),间接理由与其余文案
# 相同:显示给人看的每一个字都应当在这一个文件里能被找到。
LIST_SEPARATOR = "、"

# -- Empty states ----------------------------------------------------------

EMPTY_TERMS_TITLE = "输入任意术语即可开始查询"
EMPTY_TERMS_SUBTITLE = "词典命中不会调用模型，也不会产生费用。"
EMPTY_TERMS_NO_MATCH_TITLE = "没有找到匹配的术语"
EMPTY_TERMS_NO_MATCH_SUBTITLE = "可以换一种写法，或者直接交给模型翻译。"
EMPTY_TERMS_FAILED_TITLE = "这次查询没有成功"
EMPTY_TERMS_FAILED_SUBTITLE = "上面写明了原因。修好之后再点一次「搜索」。"
EMPTY_TRANSLATE_TITLE = "输入原文，然后点击「翻译」"
EMPTY_TRANSLATE_SUBTITLE = "命中词典的结果不调用模型；需要模型时会产生费用。"
EMPTY_STATUS_TITLE = "尚未获取服务信息"
EMPTY_STATUS_SUBTITLE = "点击「刷新」，向当前服务器地址询问版本与数据信息。"

# The three-step checklist for a client that has nothing configured. The
# state is read from the configuration and the credential store only - it
# costs no request to draw.
SETUP_STEPS_TITLE = "还差几步就能开始使用"
SETUP_STEP_BASE_URL = "① 填写服务器地址"
SETUP_STEP_TOKEN = "② 录入设备令牌"
SETUP_STEP_QUERY = "③ 开始查询"
STEP_DONE_MARK = "✓ 已完成"

# -- Endpoint change -------------------------------------------------------
# Changing the address clears all three areas. Without a word about it the
# owner reads the empty screen as data loss rather than as a deliberate
# discard of another server's answers.

ENDPOINT_CHANGED_BANNER = "服务器地址已更改，上一台服务器的结果已清除。"
ENDPOINT_CHANGED_TITLE = "已切换到新的服务器地址"
ENDPOINT_CHANGED_SUBTITLE = "重新发起请求即可获取新服务器的结果。"

# -- Banner actions and inline error surfaces ------------------------------

ACTION_RETRY = "重试"
ACTION_OPEN_SETTINGS = "打开设置"
ACTION_ENTER_TOKEN = "输入新令牌"
ACTION_DISMISS = "关闭"

# -- Standard dialog buttons -----------------------------------------------
# Qt supplies its own text for QDialogButtonBox and QMessageBox buttons, and
# that text is English unless a translator is installed. Nothing in this
# application sets it, so nothing in the static gate can see it either: the
# scan proves that every literal WE display comes from here, not that Qt's
# own defaults were replaced. These constants are what replaces them, and the
# screenshots are what proves it happened.

DIALOG_OK_BUTTON = "确定"
DIALOG_CANCEL_BUTTON = "取消"
DIALOG_YES_BUTTON = "确认"
DIALOG_NO_BUTTON = "取消"

# -- Unconfigured, said once per place -------------------------------------
# Three surfaces speak in the unconfigured state and each says a different
# thing: the chip says WHICH state, the global banner says what it COSTS, and
# the checklist says what to DO. An area's own empty card is a fourth voice,
# so it describes the area rather than repeating the checklist's heading -
# which is what it used to do, word for word, one card below it.

EMPTY_TERMS_UNCONFIGURED_TITLE = "查询结果会显示在这里"
EMPTY_STATUS_UNCONFIGURED_TITLE = "服务信息会显示在这里"
EMPTY_UNCONFIGURED_SUBTITLE = "请先按上方步骤填写服务器地址。"

# -- 凭据存储后端 -----------------------------------------------------------
# 这一行显示的原本是 keyring 库的**类名**,既不是这个程序写的字,也不在 Qt 自带
# 文案的范围里。看到它的人想知道的是「我的令牌存在哪儿」,不是哪个类实现了它,
# 所以显示中文,原始类名进 tooltip —— 排查 keyring 选错后端时一次悬停就能拿到。
# 类名到这些文案的映射在 ui/components.py,与本模块其余「服务端取值 → 文案」
# 的做法一致。

CREDENTIAL_BACKEND_WINDOWS = "Windows 凭据管理器"
CREDENTIAL_BACKEND_UNAVAILABLE = "不可用"
CREDENTIAL_BACKEND_TOOLTIP = "凭据存储实现：{backend}"

# -- 词条分类 ---------------------------------------------------------------
# 分类是服务端**数据**里的值,不是这个程序写的字 —— 所以它既不经过任何一次
# setText 的字面量检查,也不在 Qt 自带文案的范围里。改版做完、套件全绿、Qt
# 自带文案也修好之后,这一列仍然是 core_term / echo / weapon 这样的英文,
# 是逐屏看截图才发现的。
#
# 九个取值来自服务端的 CATEGORY_ORDER(src/wuwaterm/constants.py)与建库时的
# CategorySpec(src/wuwaterm/builder.py)。这里只放文案;哪个取值对应哪一条,
# 在 ui/terms_view.py 的 _CATEGORY_LABELS —— 与 _KIND_LABELS、_REASON_LABELS
# 同一处、同一形状。本模块不持有映射表,理由见模块开头。
#
# 未知分类回落到服务端给的原值(同上文件),服务端将来加一个分类,界面退化成
# 显示英文原值 —— 那是可读的,而空白或占位符不是。

CATEGORY_LABEL_CORE_TERM = "核心术语"
CATEGORY_LABEL_RESONATOR = "共鸣者"
CATEGORY_LABEL_WEAPON = "武器"
CATEGORY_LABEL_ECHO = "声骸"
CATEGORY_LABEL_SKILL = "技能"
CATEGORY_LABEL_SONATA_EFFECT = "合鸣效果"
CATEGORY_LABEL_LOCATION = "地区"
CATEGORY_LABEL_ITEM = "物品"
CATEGORY_LABEL_SPEAKER = "角色"

GLOBAL_BANNER_NOT_CONFIGURED = "尚未配置服务器地址，因此不会发出任何请求。"
GLOBAL_BANNER_INSECURE_ENDPOINT = (
    "当前服务器地址不会被使用，因此不会发出任何请求。请改用受保护的地址。"
)

FIELD_ERROR_EMPTY_INPUT = "请先输入内容。"
FIELD_ERROR_TOKEN_REQUIRED = "请输入设备令牌。"

TOOLTIP_NEEDS_ENDPOINT = "尚未配置服务器地址；请先在设置中填写。"

# -- Settings dialog -----------------------------------------------------

SETTINGS_TITLE = "设置"
SETTINGS_MENU_LABEL = "设置…"
SETTINGS_CONNECTION_SECTION_TITLE = "连接"
SETTINGS_BASE_URL_LABEL = "服务器地址"
# A NEUTRAL example, deliberately. The field used to hint
# `http://127.0.0.1:8788`, which is the exact address `ClientConfig.load` used
# to substitute whenever the stored setting was missing - the substitution that
# turned "your settings are gone" into "the server is unreachable" on the
# owner's machine, and that the unconfigured state exists to have removed. A
# placeholder is not filled in and so cannot reproduce that defect, but it does
# put the deleted suggestion back in front of the person who is deciding what
# to type. The comment above ENDPOINT_NOT_CONFIGURED forbids exactly that for
# the line one dialog away; this field is held to the same rule.
#
# Loopback is still ACCEPTED (see ERROR_MSG_INSECURE_ENDPOINT, which names it
# while explaining the rule) - it is just not what the empty field suggests.
SETTINGS_BASE_URL_PLACEHOLDER = "https://example.com/wuwaterm-api"
SETTINGS_TIMEOUT_LABEL = "请求超时（秒）"
SETTINGS_CREDENTIAL_SECTION_TITLE = "设备凭据"
SETTINGS_ENTER_TOKEN_BUTTON = "输入令牌…"
SETTINGS_CHANGE_TOKEN_BUTTON = "更改令牌…"
SETTINGS_FORGET_TOKEN_BUTTON = "遗忘令牌"
SETTINGS_TOKEN_STATUS_STORED = "已保存设备凭据。"
SETTINGS_TOKEN_STATUS_MISSING = "尚未保存设备凭据。"
SETTINGS_APPEARANCE_SECTION_TITLE = "外观"
SETTINGS_THEME_LABEL = "主题"
THEME_OPTION_SYSTEM = "跟随系统"
THEME_OPTION_LIGHT = "亮色"
THEME_OPTION_DARK = "暗色"
CREDENTIAL_STORE_ERROR_TITLE = "凭据存储"
CREDENTIAL_STORE_ERROR_MESSAGE = (
    "无法使用 Windows 凭据管理器，设备令牌没有保存。请在它可用之后重试。"
)
CREDENTIAL_STORE_FORGET_ERROR_MESSAGE = (
    "无法使用 Windows 凭据管理器，设备令牌可能仍然保存在本机。请在它可用之后重试。"
)
SETTINGS_NOT_SAVED_MESSAGE = (
    "这些设置在本次会话中已经生效，但没能写入磁盘，重启之后不会保留。"
)
SETTINGS_INVALID_BASE_URL_TITLE = "服务器地址"
SETTINGS_INVALID_BASE_URL_MESSAGE = (
    "该服务器地址不可用。纯 http:// 只允许指向本机，其他地址一律必须是 "
    "https://,以保证设备令牌绝不明文离开这台机器。例如 "
    "https://example.com/wuwaterm-api,或者 http://127.0.0.1:8788。"
)

CONFIRM_FORGET_TOKEN_TITLE = "遗忘设备凭据"
CONFIRM_FORGET_TOKEN_MESSAGE = "要从这台计算机上删除已保存的设备凭据吗？"

# -- Token entry (shared by first run and settings) -----------------------

TOKEN_DIALOG_TITLE = "输入设备令牌"
TOKEN_DIALOG_LABEL = "设备令牌"
TOKEN_DIALOG_MESSAGE = "输入新的设备令牌，替换当前保存的凭据。"
TOKEN_DIALOG_SAVE_BUTTON = "保存"
TOKEN_DIALOG_CANCEL_BUTTON = "取消"
TOKEN_SHOW_BUTTON = "显示"
TOKEN_HIDE_BUTTON = "隐藏"

# -- First-run flow -----------------------------------------------------

FIRST_RUN_TITLE = "欢迎使用 WuwaTerm"
FIRST_RUN_HEADLINE = "欢迎使用 WuwaTerm"
FIRST_RUN_MESSAGE = "请输入服务运营方发给你的设备令牌以继续。"
FIRST_RUN_STORAGE_NOTE = "令牌保存在 Windows 凭据管理器中，不会写进配置文件。"
FIRST_RUN_TOKEN_PLACEHOLDER = "在此粘贴设备令牌"
FIRST_RUN_CONTINUE_BUTTON = "继续"
FIRST_RUN_QUIT_BUTTON = "退出"

# -- Error / status messages -----------------------------------------------
# Keys mirror the stable error codes in errors.py (server codes plus the
# client-only transport/cancellation states). Message text lives only here.
# Every one of them is worded differently from every other: two codes that
# read the same send the owner to the same action, and the point of keeping
# fifteen of them apart is that they do not.

ERROR_MSG_UNAUTHORIZED = "已保存的设备凭据被拒绝。请输入一个新的设备令牌。"
ERROR_MSG_FORBIDDEN = "这个设备凭据没有执行该操作的权限。"
ERROR_MSG_RATE_LIMITED = "请求过于频繁。请稍等片刻再试。"
ERROR_MSG_PAYLOAD_TOO_LARGE = "这段内容体积过大，无法发送。"
ERROR_MSG_INVALID_REQUEST = "这个请求不合法。"
ERROR_MSG_INPUT_TOO_LONG = "文本长度超过服务允许的上限。"
ERROR_MSG_LLM_UNAVAILABLE = "翻译模型当前不可用。"
ERROR_MSG_LLM_BUDGET_EXHAUSTED = "翻译模型的调用额度暂时已经用尽。"
ERROR_MSG_INTERNAL = "服务报告了一个内部错误。"
ERROR_MSG_OFFLINE = "无法连接到服务器。请检查服务器地址与网络连接。"
ERROR_MSG_TIMEOUT = "请求超时。"
ERROR_MSG_UNKNOWN = "发生了意料之外的错误。"
ERROR_MSG_INSECURE_ENDPOINT = (
    "该服务器地址会把设备令牌在没有传输保护的情况下送往另一台机器，因此"
    "没有发出任何请求。请改用 https:// 地址；http:// 只允许指向本机。"
)
ERROR_MSG_NOT_CONFIGURED = (
    "尚未配置服务器地址，因此没有发出任何请求。请在设置中填写服务器地址。"
)
