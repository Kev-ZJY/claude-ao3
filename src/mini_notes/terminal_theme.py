"""Host-default terminal colors and deliberate low-color fallbacks.

The Rich console reports its negotiated/environment color capability. No OSC probe
or guess about the host's RGB background is required to preserve ANSI defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from textual.theme import Theme


def terminal_is_dark() -> bool:
    hint = os.environ.get('COLORFGBG', '').split(';')[-1]
    try:
        value = int(hint)
    except ValueError:
        return True  # Only a hint for accents/blocks; auto never paints the canvas.
    return value not in (7, 15) and value < 232 or 232 <= value < 244


@dataclass(frozen=True)
class TerminalPalette:
    foreground: str
    background: str
    muted: str
    accent: str
    block: str
    block_foreground: str

    def tuple(self) -> tuple[str, str, str, str, str]:
        return self.foreground, self.background, self.muted, self.accent, self.block


def palette_for(mode: str, capability: str | None, dark_hint: bool) -> TerminalPalette:
    dark = dark_hint if mode == 'auto' else mode == 'dark'
    # Gray RGB backgrounds are used only when the terminal can actually show them.
    rich_color = capability in ('truecolor', '256', 'eight_bit')
    if mode == 'auto':
        return TerminalPalette('default', 'default', 'default',
            'bright_magenta' if dark else 'magenta',
            '#ECECEC' if rich_color else 'white',
            '#272727' if rich_color else 'black')
    if rich_color:
        return TerminalPalette('#E5E5E5', '#202020', '#A6A6A6', '#B3B4FF', '#454545', '#E5E5E5') if dark else TerminalPalette('#272727', '#F7F7F7', '#666666', '#5554A5', '#ECECEC', '#272727')
    return TerminalPalette('bright_white', 'black', 'white', 'bright_magenta', 'bright_black', 'bright_white') if dark else TerminalPalette('black', 'bright_white', 'bright_black', 'magenta', 'white', 'black')


def _css(color: str) -> str:
    return color if color.startswith('#') else 'ansi_' + color


def theme_for(mode: str, capability: str | None, dark_hint: bool) -> Theme:
    palette = palette_for(mode, capability, dark_hint)
    fg, bg, _, accent, block = map(_css, palette.tuple())
    return Theme(name='mini-' + ('host' if mode == 'auto' else mode), ansi=True,
        primary=accent, secondary=accent, accent=accent,
        foreground=fg, background=bg, surface=bg, panel=bg,
        dark=dark_hint if mode == 'auto' else mode == 'dark',
        variables={
            'ansi-background': bg, 'ansi-foreground': fg,
            'text': fg, 'text-muted': fg + ' 60%',
            'block-cursor-foreground': _css(palette.block_foreground),
            'block-cursor-background': block,
            'block-cursor-text-style': 'bold',
            'block-hover-background': block,
            'input-selection-background': block,
            'input-selection-foreground': _css(palette.block_foreground),
            'input-cursor-foreground': bg, 'input-cursor-background': fg,
            'scrollbar': _css(palette.muted), 'scrollbar-background': bg,
            'scrollbar-background-hover': bg, 'scrollbar-background-active': bg,
            'scrollbar-corner-color': bg, 'border': _css(palette.muted),
            'border-blurred': _css(palette.muted),
        })
