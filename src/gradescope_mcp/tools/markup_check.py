"""Advisory Markdown/KaTeX checks for text the server is about to write.

Gradescope renders comments and rubric descriptions as Markdown with
KaTeX math (``$...$`` inline, ``$$...$$`` display). Malformed delimiters do
not fail the write; they show students raw ``$`` signs or swallow the rest of
the text. These checks only warn: they never block a write, because a lone
``$`` is also a legitimate currency sign.
"""

import re

_ESCAPED_DOLLAR = re.compile(r"\\\$")
_CODE = re.compile(r"`[^`]*`")


def _brace_problem(segment: str) -> bool:
    depth = 0
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "\\":
            i += 2  # \{ and \} are literal braces
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return True
        i += 1
    return depth != 0


def math_markup_warnings(text) -> list[str]:
    """Return human-readable warnings about math delimiters in ``text``.

    An empty list means nothing suspicious was found. Never raises for
    non-string input (returns ``[]``).
    """
    if not isinstance(text, str) or "$" not in text and "\\(" not in text and "\\[" not in text:
        return []
    warnings: list[str] = []
    body = _CODE.sub("", _ESCAPED_DOLLAR.sub("", text))

    if re.search(r"\${3,}", body):
        warnings.append(
            "three or more consecutive '$' (e.g. '$$$$'): math looks wrapped in "
            "'$$' twice; KaTeX will show raw dollar signs"
        )
    display = body.count("$$")
    if display % 2:
        warnings.append("odd number of '$$' display-math delimiters (unclosed block)")

    # Single-dollar inline math, after removing display delimiters.
    inline_body = body.replace("$$", "\x00")
    if inline_body.count("$") % 2:
        warnings.append(
            "odd number of single '$' (unclosed inline math, or a currency sign "
            "that should be written '\\$')"
        )

    for opener, closer, name in (("\\(", "\\)", "\\( \\)"), ("\\[", "\\]", "\\[ \\]")):
        if body.count(opener) != body.count(closer):
            warnings.append(f"unbalanced {name} math delimiters")

    if not display % 2 and not inline_body.count("$") % 2:
        segments = re.findall(r"\$\$(.*?)\$\$", body, flags=re.S)
        segments += re.findall(r"\$([^$]*)\$", re.sub(r"\$\$.*?\$\$", "", body, flags=re.S))
        if any(_brace_problem(s) for s in segments):
            warnings.append("unbalanced '{' '}' inside a math expression")
    return warnings


def markup_warnings_for(fields: dict) -> dict[str, list[str]]:
    """Map each named text field with problems to its warnings."""
    out = {}
    for name, value in fields.items():
        found = math_markup_warnings(value)
        if found:
            out[name] = found
    return out
