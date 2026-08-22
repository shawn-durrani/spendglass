"""Phone-width view (#26): the decisions that make both pages work at
375px, pinned as strings on the served templates.

The full check ran in a real browser at 375x812 (gate, table, column
popover, review, admin, /viz) with document scrollWidth asserted equal to
the viewport on every state. What CI can hold without a browser is the
shape of those fixes: the media blocks exist, the touch affordances are
declared, and the one crush-guard width stays put.
"""

from spendglass.ui import PAGE, PAGE_VIZ


def test_both_pages_declare_a_device_viewport():
    for page in (PAGE, PAGE_VIZ):
        assert '<meta name="viewport" content="width=device-width' in page


def test_both_pages_carry_the_phone_width_block():
    for page in (PAGE, PAGE_VIZ):
        assert "@media(max-width:640px)" in page
        assert "@media(pointer:coarse)" in page


def test_touch_gets_no_resize_hotspot_and_a_visible_copy_button():
    """Header resize is a 9px mouse hotspot that fights touch scrolling, so
    coarse pointers lose it (panning the tablewrap replaces it). The copy
    button was hover-revealed, which is invisible on touch."""
    block = PAGE.split("@media(pointer:coarse){", 1)[1].split("\n}", 1)[0]
    assert "th .rz{display:none}" in block
    assert ".copy{opacity:.55}" in block


def test_review_table_pans_instead_of_crushing():
    """#rvtbl is width:100% with ~784px of fixed columns; below that the
    descriptor column silently collapses to nothing. The phone block gives
    it a floor and .tablewrap provides the panning."""
    assert "#rvtbl{min-width:960px}" in PAGE


def test_main_header_wraps_via_class_not_inline_style():
    """The header's layout moved from an inline style to .topbar so the
    phone block can reach it - an inline flex row can never wrap under a
    media query."""
    assert ".topbar{display:flex;justify-content:space-between" in PAGE
    assert '<div class="topbar">' in PAGE
    assert 'style="display:flex;justify-content:space-between' not in PAGE


def test_gate_buttons_go_full_width_on_small_screens():
    assert "#gate button:not(.link){width:100%}" in PAGE
