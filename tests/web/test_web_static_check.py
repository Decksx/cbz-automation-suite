"""Static checks on the frontend (design §9 "Output", §11, §12).

The CSP forbids inline script and eval, and the console renders untrusted text
(file names, log lines) everywhere, so these sinks are banned outright rather
than reviewed case by case.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "comic_automation" / "web" / "static"
BANNED_JS = re.compile(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\s*\(|new\s+Function\s*\()")
# Single-, double- and back-quoted JS string literals, with backslash escapes.
STRINGS = re.compile(r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`""")
JS_FILES = sorted(STATIC.rglob("*.js"))
HTML_FILES = sorted(STATIC.rglob("*.html"))


def test_there_is_something_to_check():
    assert JS_FILES and HTML_FILES


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_no_html_injection_sinks_or_eval_in_javascript(path):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        # The ban is on code; comments may name the sinks to warn against them.
        # Blank out string literals first, so a "//" inside a URL string does
        # not hide the rest of the line as if it were a comment.
        code = STRINGS.sub('""', line)
        code = code.split("//", 1)[0]
        assert not BANNED_JS.search(code), f"{path.name}:{number}: {line.strip()}"


def test_the_comment_stripping_does_not_hide_code_after_a_url_string():
    line = 'fetch("http://x"); el.innerHTML = name;'
    code = STRINGS.sub('""', line).split("//", 1)[0]
    assert BANNED_JS.search(code)


@pytest.mark.parametrize("path", HTML_FILES, ids=lambda p: p.name)
def test_html_has_no_inline_script_style_or_handlers(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), "inline <script> (blocked by the CSP)"
    assert not re.search(r"\sstyle\s*=", text), "inline style attribute (blocked by the CSP)"
    assert not re.search(r"\son[a-z]+\s*=", text, re.IGNORECASE), "inline event handler (blocked by the CSP)"
    assert "http://" not in text and "https://" not in text, "external resource (the CSP is 'self' only)"


def _luminance(hex_colour: str) -> float:
    rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_text_tokens_meet_wcag_aa_on_every_surface():
    css = (STATIC / "css" / "console.css").read_text(encoding="utf-8")
    tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", css))
    for fg in ("text", "muted", "accent", "ok", "warn", "info"):
        for bg in ("bg", "panel", "panel-2"):
            ratio = _contrast(tokens[fg], tokens[bg])
            assert ratio >= 4.5, f"--{fg} on --{bg} is {ratio:.2f}:1"
