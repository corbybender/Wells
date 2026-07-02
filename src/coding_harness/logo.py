"""Wells logo, pre-rendered as Rich half-block art.

Generated from wells_logo.png (scripts kept out of runtime: no Pillow needed
here). Each character cell encodes two vertical pixels via ▀/▄ with
truecolor markup; transparent pixels render as terminal background.
"""

LOGO_MARKUP_LINES = [
    '    [rgb(120,132,141)]▄[/][rgb(116,128,136) on rgb(129,142,151)]▀[/][rgb(115,127,135) on rgb(126,140,149)]▀[/][rgb(109,121,129) on rgb(125,136,145)]▀[/][rgb(124,132,141)]▄[/][rgb(110,122,129)]▄[/]',
    '   [rgb(107,118,128) on rgb(103,115,123)]▀[/][rgb(120,132,141) on rgb(107,119,128)]▀[/][rgb(118,130,138) on rgb(92,105,113)]▀[/][rgb(121,131,140) on rgb(94,100,109)]▀[/][rgb(76,99,108) on rgb(33,90,101)]▀[/][rgb(62,101,109) on rgb(6,98,109)]▀[/][rgb(106,117,125) on rgb(82,100,109)]▀[/]',
    '   [rgb(94,106,114)]▀[/][rgb(116,128,137) on rgb(57,69,76)]▀[/][rgb(111,123,132) on rgb(85,97,106)]▀[/][rgb(114,124,133) on rgb(89,101,109)]▀[/][rgb(76,101,110) on rgb(77,86,94)]▀[/][rgb(53,90,100) on rgb(77,85,93)]▀[/][rgb(90,102,110)]▀[/]',
    '    [rgb(81,92,100) on rgb(93,104,112)]▀[/][rgb(69,81,90) on rgb(54,64,71)]▀[/][rgb(87,98,106) on rgb(89,100,109)]▀[/][rgb(110,122,130) on rgb(113,125,134)]▀[/][rgb(99,112,121) on rgb(80,91,99)]▀[/][rgb(71,83,92) on rgb(85,96,104)]▀[/]',
    '  [rgb(79,88,98)]▄[/][rgb(93,104,112) on rgb(76,87,95)]▀[/] [rgb(65,75,83)]▄[/][rgb(70,80,88)]▀[/]  [rgb(74,84,92)]▀[/][rgb(87,99,106) on rgb(72,82,90)]▀[/][rgb(71,82,90)]▄[/]',
    '  [rgb(82,92,101) on rgb(94,106,115)]▀[/]  [rgb(62,72,80) on rgb(70,79,87)]▀[/]     [rgb(80,90,98) on rgb(94,106,114)]▀[/]',
    ' [rgb(90,102,109) on rgb(89,102,110)]▀[/][rgb(90,102,110)]▀[/]  [rgb(66,77,84) on rgb(59,68,77)]▀[/]     [rgb(88,99,109) on rgb(85,97,107)]▀[/]',
    '[rgb(75,85,94)]▄[/][rgb(82,93,102) on rgb(71,81,90)]▀[/]          [rgb(78,89,98) on rgb(78,89,98)]▀[/]',
]


def logo_lines(max_width: int = 0) -> list[str]:
    """Return the logo's Rich markup lines; empty when the terminal is too narrow."""
    if max_width and max_width < 18:
        return []
    return list(LOGO_MARKUP_LINES)
