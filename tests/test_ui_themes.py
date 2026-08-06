"""The five named themes are actually present and complete.

These are structural checks on the served page, not taste: a theme that
silently omits a token inherits the previous palette's value, which is how
a money figure or a staleness banner goes low-contrast on one theme only.
"""

import re

import pytest

from spendglass.ui import PAGE

THEMES = ["midnight", "espresso", "oled", "light"]  # zinc is :root
# Money and warning tokens carry meaning; they must be stated per theme.
SEMANTIC = ["--credit", "--debit", "--warn-bg", "--warn-fg", "--accent-ink"]


def _block(theme: str) -> str:
    m = re.search(r'\[data-theme="%s"\]\{(.*?)\}' % theme, PAGE, re.S)
    assert m, f"no [data-theme={theme}] block"
    return m.group(1)


def _root() -> str:
    m = re.search(r"^:root\{(.*?)\}", PAGE, re.S | re.M)
    assert m, "no :root token block"
    return m.group(1)


def test_all_five_themes_offered_in_both_pickers():
    for theme in ["zinc", *THEMES]:
        assert f'value="{theme}"' in PAGE
    # gate + main each carry a picker, so a theme is choosable before login
    assert PAGE.count('class="theme-pick"') == 2


def test_root_defines_every_semantic_token():
    root = _root()
    for token in [*SEMANTIC, "--bg", "--fg", "--card", "--line", "--accent", "--ok"]:
        assert token in root, f":root is missing {token}"


@pytest.mark.parametrize("theme", THEMES)
def test_every_theme_defines_its_own_surfaces(theme):
    """A palette must at minimum restate the surfaces it changes, or it is
    half-ported — a card that keeps the previous theme's background is the
    most visible way a theme breaks."""
    block = _block(theme)
    for token in ("--bg", "--card", "--line", "--fg"):
        assert token in block, f"[data-theme={theme}] does not set {token}"


@pytest.mark.parametrize("theme", THEMES)
def test_lightness_flip_restates_meaning_carrying_tokens(theme):
    """The real bug class: a theme that inverts the surfaces but INHERITS the
    money and warning colours. Green-on-dark is not the green that reads on
    white. Dark variants may inherit from :root — a theme that flips to
    light surfaces may not."""
    block = _block(theme)
    flips_light = "--bg:#fff" in block or "--bg:#f" in block
    if not flips_light:
        return
    for token in SEMANTIC:
        assert token in block, (
            f"[data-theme={theme}] inverts to light surfaces but inherits "
            f"{token} from the dark :root — it will be low-contrast")


def test_light_theme_inverts_surfaces_not_just_text():
    """The light palette must actually be light — a half-ported theme that
    keeps a dark --bg is worse than no theme at all."""
    light = _block("light")
    assert "--bg:#fff" in light
    assert "--fg:#101012" in light


def test_theme_applied_before_first_paint():
    """No flash of the wrong palette: the attribute is set by an inline
    script in <body>, not after the app boots."""
    pre_paint = PAGE.index('localStorage.getItem("sg-theme")')
    boot = PAGE.index("boot()")
    assert pre_paint < boot
    assert "zinc" in PAGE[pre_paint:pre_paint + 200], (
        "the pre-paint script must treat zinc as the attribute-less default")


def test_no_hardcoded_scheme_media_query_remains():
    """The OS-follows-you behaviour is replaced by an explicit choice; a
    leftover media query would fight the picker."""
    assert "prefers-color-scheme" not in PAGE


def test_picker_is_labelled_for_screen_readers():
    assert PAGE.count('aria-label="Colour theme"') == 2
    assert PAGE.count("Colour theme</label>") == 2


# ── contrast: the check that makes "same colours as the other apps" real ────

def _tokens(block: str) -> dict:
    return dict(re.findall(r"(--[a-z-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", block))


def _rgb(h: str) -> tuple:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _relative_luminance(h: str) -> float:
    def chan(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in _rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    a, b = _relative_luminance(fg), _relative_luminance(bg)
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


AA = 4.5  # WCAG AA for normal-size text


@pytest.mark.parametrize("theme", ["zinc", *THEMES])
def test_money_and_warnings_stay_legible_in_every_theme(theme):
    """The point of the issue, made checkable. A palette is only 'ported' if
    an amount and a staleness warning still READ on it — measured, not
    eyeballed, so a future palette tweak can't quietly drop one below AA."""
    tok = dict(_tokens(_root()))
    if theme != "zinc":
        tok.update(_tokens(_block(theme)))
    pairs = {
        "credit on card": ("--credit", "--card"),
        "debit on card": ("--debit", "--card"),
        "warning text on warning band": ("--warn-fg", "--warn-bg"),
        "button ink on accent": ("--accent-ink", "--accent"),
        "body text on background": ("--fg", "--bg"),
        "muted text on card": ("--muted", "--card"),
    }
    for what, (fg, bg) in pairs.items():
        assert fg in tok and bg in tok, f"{theme}: {fg}/{bg} undefined"
        got = contrast(tok[fg], tok[bg])
        assert got >= AA, f"{theme}: {what} is {got:.2f}:1, below AA ({AA}:1)"
