import pytest
from rich.color import Color
from mini_notes.terminal_theme import palette_for, theme_for


@pytest.mark.parametrize('capability', [None, 'standard', '256', 'truecolor'])
@pytest.mark.parametrize('dark', [False, True])
def test_auto_uses_host_defaults_for_canvas(capability, dark):
    palette = palette_for('auto', capability, dark)
    assert palette.foreground == palette.background == 'default'
    assert theme_for('auto', capability, dark).background == 'ansi_default'


@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_sixteen_color_manual_background_never_maps_to_blue(mode):
    palette = palette_for(mode, 'standard', True)
    assert palette.background in ('black', 'bright_white')
    color = Color.parse(palette.background)
    assert color.number not in (4, 12)


@pytest.mark.parametrize('dark_hint', [False, True])
def test_auto_shaded_blocks_use_light_gray_and_paired_dark_ink(dark_hint):
    palette = palette_for('auto', 'standard', dark_hint)
    assert palette.background == palette.foreground == 'default'
    assert palette.block == 'white' and palette.block_foreground == 'black'
    assert palette.block != 'bright_black'


@pytest.mark.parametrize('mode', ['auto', 'light'])
def test_light_blocks_are_lighter_than_previous_gray(mode):
    palette = palette_for(mode, 'truecolor', True)
    assert Color.parse(palette.block).get_truecolor().red >= 230
    assert Color.parse(palette.block_foreground).get_truecolor().red < 64
