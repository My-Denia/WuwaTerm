"""HTML for the owner-private web presentation layer.

Rendered from Python rather than from a template engine, because adding one
would add a runtime dependency to a process whose dependency set is currently
httpx + fastapi + uvicorn, and this surface is two views. The trade is that
escaping is this module's responsibility instead of a framework's, so there is
exactly one rule here and it has no exceptions: EVERY value that did not
originate in this file goes through ``esc``. Nothing is interpolated raw, and
no caller-supplied value is ever placed anywhere but element text or a quoted
attribute value.

The page is also entirely self-contained - no external stylesheet, no font
download, no script from anywhere. A phone browser on a slow mobile connection
renders it in one round trip, and there is no third-party origin involved in a
surface whose whole point is that it is private.
"""

from __future__ import annotations

from html import escape as _escape

from wuwaterm.application import (
    DIRECTION_TO_CHINESE,
    DIRECTION_TO_ENGLISH,
    KIND_ERROR,
    KIND_EXACT,
    KIND_FUZZY,
    KIND_LLM,
    KIND_NOOP,
)

# The pipeline's OWN vocabulary, imported. An earlier revision of this file
# invented two of its own - it tested `kind == "dictionary"` when the pipeline
# emits "exact"/"fuzzy", and mapped directions "zh2en"/"en2zh" when the
# pipeline emits "zh"/"en". Neither invented key ever matched, so every answer
# was labelled as coming from the model and the direction was printed raw. On a
# product whose promise is "official dictionary first, model only on a miss",
# labelling a dictionary hit as a model translation is not a cosmetic bug: it
# misreports provenance, which is the one thing the label exists to carry.
_SOURCE_LABELS = {
    KIND_EXACT: "词典 · 官方译名",
    KIND_FUZZY: "词典 · 近似匹配",
    KIND_LLM: "模型",
    KIND_NOOP: "无可翻译内容",
}
# KIND_ERROR never reaches a view: the handler turns it into an ApiError and the
# error page renders it. Named here rather than merely absent, so the coverage
# assertion can require this map to account for EVERY kind the pipeline defines
# and this one is an explicit exemption instead of an oversight.
_KINDS_NOT_RENDERED_AS_RESULTS = frozenset({KIND_ERROR})
# The pipeline's own message for a submission that normalised to nothing is
# English, and this interface is Chinese. Replaced rather than passed through.
_NOOP_MESSAGE = "输入中没有可翻译的内容。"
_DIRECTION_LABELS = {
    DIRECTION_TO_CHINESE: "译为中文",
    DIRECTION_TO_ENGLISH: "译为英文",
}


def esc(value: object) -> str:
    """Escape a value for HTML text or a quoted attribute.

    ``quote=True`` matters: it escapes the double quote as well, which is what
    makes interpolation into ``attr="..."`` safe. The default would not.
    """
    return _escape("" if value is None else str(value), quote=True)


# Type sizing is set for reading Chinese on a phone held in one hand.
#
# 16px on the inputs is not an aesthetic choice: iOS Safari zooms the viewport
# when a focused input's font-size is below 16px, which throws the layout off
# centre on the first tap into the search box and cannot be undone by the page.
#
# line-height is 1.75 rather than the ~1.5 that reads well for Latin text.
# Chinese glyphs occupy the full em box with no x-height relief, so lines set
# tighter than about 1.7 visually merge; the value is set on the body so that
# every block inherits it.
_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f4f1ea;
  --panel: #fffdf7;
  --ink: #23201a;
  --muted: #6e6858;
  --line: #e0dacb;
  --accent: #8a6d3b;
  --accent-ink: #fffdf7;
  --accent-soft: rgba(138, 109, 59, 0.18);
  --warn-bg: #f9efec;
  --warn-line: #d8b4ac;
  --warn-ink: #7c2f26;
  --radius: 3px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1113;
    --panel: #16191d;
    --ink: #e9e6dc;
    --muted: #9a937f;
    --line: #282d34;
    --accent: #c9a86a;
    --accent-ink: #17130a;
    --accent-soft: rgba(201, 168, 106, 0.16);
    --warn-bg: #221414;
    --warn-line: #5c3636;
    --warn-ink: #e0a49c;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  /* The one piece of chrome with no DOM node: a brand bar across the top of
     the viewport, in the gold the whole palette hangs on. */
  border-top: 3px solid var(--accent);
  background: var(--bg);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC",
    "Source Han Sans SC", sans-serif;
  font-size: 16px;
  line-height: 1.75;
  -webkit-text-size-adjust: 100%;
}
::selection { background: var(--accent-soft); }
.wrap { max-width: 40rem; margin: 0 auto; padding: 0.5rem 1rem 3rem; }
header { padding: 1.1rem 0 0.9rem; }
h1 { font-size: 1.45rem; margin: 0; letter-spacing: 0.06em; font-weight: 700; }
h1::before {
  content: "◆";
  color: var(--accent);
  font-size: 0.8em;
  margin-right: 0.55rem;
  vertical-align: 0.08em;
}
header p {
  margin: 0.3rem 0 0;
  color: var(--muted);
  font-size: 0.78rem;
  letter-spacing: 0.14em;
}
nav {
  display: flex;
  gap: 1.5rem;
  margin-bottom: 1.4rem;
  border-bottom: 1px solid var(--line);
}
nav a {
  padding: 0.5rem 0.15rem;
  margin-bottom: -1px;
  color: var(--muted);
  text-decoration: none;
  font-size: 0.95rem;
  letter-spacing: 0.06em;
  border-bottom: 2px solid transparent;
}
nav a[aria-current="page"] {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
form { margin: 0 0 1.25rem; }
label {
  display: block;
  font-size: 0.8rem;
  letter-spacing: 0.1em;
  color: var(--muted);
  margin-bottom: 0.45rem;
}
input[type="text"], textarea {
  width: 100%;
  font: inherit;
  font-size: 16px;
  color: var(--ink);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 0.7rem 0.8rem;
}
input[type="text"]:focus-visible, textarea:focus-visible {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
textarea { min-height: 7rem; resize: vertical; line-height: 1.75; }
input[type="text"]::placeholder, textarea::placeholder {
  color: var(--muted);
  opacity: 0.75;
}
input[type="text"], textarea { caret-color: var(--accent); }
button {
  margin-top: 0.85rem;
  width: 100%;
  /* 44px is the smallest reliably tappable target on a phone. */
  min-height: 2.75rem;
  font: inherit;
  font-size: 0.95rem;
  font-weight: 600;
  letter-spacing: 0.35em;
  text-indent: 0.35em;
  color: var(--accent-ink);
  background: var(--accent);
  border: 0;
  border-radius: var(--radius);
}
button:hover { filter: brightness(1.07); }
button:active { filter: brightness(0.94); }
/* The page has no scripts, so state changes are all CSS-driven; keep them
   instant for users who ask the OS for reduced motion. */
@media (prefers-reduced-motion: no-preference) {
  input[type="text"], textarea {
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  button { transition: filter 0.15s ease; }
  nav a { transition: color 0.15s ease, border-bottom-color 0.15s ease; }
}
button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  /* The result slab wears the accent on its top edge: provenance is this
     product's promise, so the surface that carries it gets the brand mark. */
  border-top: 2px solid var(--accent);
  border-radius: var(--radius);
  padding: 1rem 1.1rem;
  margin-bottom: 0.9rem;
}
.card h2 {
  font-size: 0.72rem;
  letter-spacing: 0.12em;
  color: var(--accent);
  margin: 0 0 0.55rem;
  font-weight: 600;
}
.card h2::before {
  content: "—";
  margin-right: 0.45rem;
  opacity: 0.75;
}
/* Chinese has no inter-word spaces, so the default break-on-space rule leaves a
   long run of Han characters overflowing its container on a narrow screen.
   anywhere lets the line break between glyphs, which is how Chinese wraps. */
/* pre-wrap, because the shared pipeline joins translated chunks with newlines
   and the browser's default collapsing turned dialogue, paragraphs and chunked
   results into one continuous block - discarding structure the translation had
   actually preserved. */
/* The translation itself is the hero content of the page; it sets one step
   above body size on purpose. */
.result { margin: 0; overflow-wrap: anywhere; white-space: pre-wrap; font-size: 1.04rem; }
.term { display: flex; flex-wrap: wrap; gap: 0.35rem 0.75rem; padding: 0.65rem 0; border-top: 1px solid var(--line); overflow-wrap: anywhere; }
.term:first-of-type { border-top: 0; padding-top: 0.1rem; }
.term .zh { font-weight: 600; }
.term .en { color: var(--accent); overflow-wrap: anywhere; }
.term .meta { color: var(--muted); font-size: 0.78rem; width: 100%; letter-spacing: 0.03em; opacity: 0.85; }
.empty { color: var(--muted); margin: 0; }
.error {
  background: var(--warn-bg);
  border: 1px solid var(--warn-line);
  color: var(--warn-ink);
  border-radius: var(--radius);
  padding: 0.8rem 1rem;
  margin-bottom: 0.75rem;
  overflow-wrap: anywhere;
}
footer {
  margin-top: 2.25rem;
  padding-top: 0.9rem;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 0.78rem;
  letter-spacing: 0.04em;
}
footer::before {
  content: "◆";
  color: var(--accent);
  font-size: 0.6rem;
  display: block;
  text-align: center;
  margin-bottom: 0.5rem;
}
"""

_TAB_LOOKUP = "lookup"
_TAB_TRANSLATE = "translate"


def _nav(mount: str, active: str) -> str:
    items = ((_TAB_LOOKUP, "查词"), (_TAB_TRANSLATE, "翻译"))
    parts = []
    for key, label in items:
        current = ' aria-current="page"' if key == active else ""
        href = f"{mount}/" if key == _TAB_LOOKUP else f"{mount}/{key}"
        parts.append(f'<a href="{esc(href)}"{current}>{esc(label)}</a>')
    return "<nav>" + "".join(parts) + "</nav>"


def page(*, mount: str, active: str, body: str) -> str:
    """The full document. ``body`` is already-rendered, already-escaped HTML."""
    title = "鸣潮术语" if active == _TAB_LOOKUP else "鸣潮翻译"
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        # width=device-width is what makes the phone lay the page out at its own
        # width instead of at a desktop default and then shrinking it.
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        # This surface is private. Telling well-behaved crawlers not to index it
        # is not a security control - the edge is - but it costs one line.
        '<meta name="robots" content="noindex, nofollow">\n'
        f"<title>{esc(title)}</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="wrap">\n'
        "<header>\n"
        f"<h1>{esc(title)}</h1>\n"
        "<p>私用工具，仅限本人使用。</p>\n"
        "</header>\n"
        f"{_nav(mount, active)}\n"
        f"{body}\n"
        "<footer>词条以官方译名为准；句子翻译在词典未命中时才会调用模型。"
        "查词仅精确匹配、翻译方向自动判定，均为有意保持极简。"
        "需要模糊候选或指定方向时请用 HTTP API。</footer>\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


def error_block(message: str) -> str:
    return f'<div class="error" role="alert">{esc(message)}</div>'


def lookup_view(*, mount: str, query: str = "", matches=None, searched: bool = False) -> str:
    form = (
        f'<form method="post" action="{esc(mount)}/lookup">\n'
        '<label for="q">查询词条</label>\n'
        '<input type="text" id="q" name="q" inputmode="search" '
        'autocomplete="off" autocapitalize="off" spellcheck="false" '
        f'placeholder="输入中文或英文词条" value="{esc(query)}">\n'
        "<button type=\"submit\">查词</button>\n"
        "</form>\n"
    )
    if not searched:
        return form
    rows = []
    for match in matches or ():
        meta_bits = []
        if getattr(match, "category", None):
            meta_bits.append(str(match.category))
        if getattr(match, "reason", None):
            meta_bits.append(str(match.reason))
        meta = f'<span class="meta">{esc(" · ".join(meta_bits))}</span>' if meta_bits else ""
        rows.append(
            '<div class="term">'
            f'<span class="zh">{esc(match.zh)}</span>'
            f'<span class="en">{esc(match.en)}</span>'
            f"{meta}"
            "</div>"
        )
    if rows:
        body = f'<div class="card"><h2>词典结果</h2>{"".join(rows)}</div>'
    else:
        body = (
            '<div class="card"><h2>词典结果</h2>'
            '<p class="empty">词典中没有这个词条。可以到「翻译」里让模型处理整句。</p>'
            "</div>"
        )
    return form + body


def translate_view(
    *, mount: str, text: str = "", result=None, translated: bool = False
) -> str:
    form = (
        f'<form method="post" action="{esc(mount)}/translate">\n'
        '<label for="text">待翻译文本</label>\n'
        f'<textarea id="text" name="text" placeholder="粘贴或输入一句话">{esc(text)}</textarea>\n'
        "<button type=\"submit\">翻译</button>\n"
        "</form>\n"
    )
    if not translated or result is None:
        return form
    kind = str(getattr(result, "kind", ""))
    raw_direction = str(getattr(result, "direction", ""))
    if kind == KIND_NOOP:
        # Not a translation, so it does not get a translation's heading: no
        # direction, and the pipeline's English notice replaced by a Chinese
        # one. Rendered before the direction lookup below because "translated
        # into English" is a false statement about an empty result.
        return form + (
            '<div class="card">'
            f'<h2>{esc(_SOURCE_LABELS[KIND_NOOP])}</h2>'
            f'<p class="result">{esc(_NOOP_MESSAGE)}</p>'
            "</div>"
        )
    # An unrecognised kind claims NOTHING about provenance rather than
    # defaulting to one of the two answers. Guessing here is how the previous
    # revision came to label every dictionary hit as a model translation.
    source = _SOURCE_LABELS.get(kind, kind)
    direction = _DIRECTION_LABELS.get(raw_direction, raw_direction)
    heading = " · ".join(part for part in (source, direction) if part)
    body = (
        '<div class="card">'
        f'<h2>{esc(heading)}</h2>'
        f'<p class="result">{esc(getattr(result, "text", ""))}</p>'
        "</div>"
    )
    return form + body
