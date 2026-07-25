from __future__ import annotations

COLOR_HUES: list[tuple[str, str, str]] = [
    ("red", "Red", "🔴"),
    ("orange", "Orange", "🟠"),
    ("yellow", "Yellow", "🟡"),
    ("green", "Green", "🟢"),
    ("cyan", "Cyan", "🔵"),
    ("blue", "Blue", "🔷"),
    ("purple", "Purple", "🟣"),
    ("pink", "Pink", "🩷"),
    ("white", "White", "⚪"),
    ("gray", "Gray", "⬜"),
    ("black", "Black", "⚫"),
]

_SHADES: dict[str, list[tuple[str, str]]] = {
    "red": [
        ("#FF0000", "Pure Red"),
        ("#DC143C", "Crimson"),
        ("#B22222", "Firebrick"),
        ("#8B0000", "Dark Red"),
        ("#FF4500", "Orange Red"),
        ("#CD5C5C", "Indian Red"),
    ],
    "orange": [
        ("#FFA500", "Orange"),
        ("#FF8C00", "Dark Orange"),
        ("#FF7F50", "Coral"),
        ("#FF6347", "Tomato"),
        ("#FFD700", "Gold"),
    ],
    "yellow": [
        ("#FFFF00", "Pure Yellow"),
        ("#FFD700", "Gold"),
        ("#FFFACD", "Lemon Chiffon"),
        ("#FFFFE0", "Light Yellow"),
        ("#DAA520", "Goldenrod"),
    ],
    "green": [
        ("#00FF00", "Pure Green"),
        ("#32CD32", "Lime Green"),
        ("#228B22", "Forest Green"),
        ("#008000", "Green"),
        ("#006400", "Dark Green"),
        ("#7CFC00", "Lawn Green"),
    ],
    "cyan": [
        ("#00FFFF", "Cyan"),
        ("#00CED1", "Dark Turquoise"),
        ("#20B2AA", "Light Sea Green"),
        ("#48D1CC", "Medium Turquoise"),
        ("#00BFFF", "Deep Sky Blue"),
    ],
    "blue": [
        ("#0000FF", "Pure Blue"),
        ("#0000CD", "Medium Blue"),
        ("#00008B", "Dark Blue"),
        ("#4169E1", "Royal Blue"),
        ("#6495ED", "Cornflower Blue"),
        ("#1E90FF", "Dodger Blue"),
    ],
    "purple": [
        ("#800080", "Purple"),
        ("#8A2BE2", "Blue Violet"),
        ("#9370DB", "Medium Purple"),
        ("#9400D3", "Dark Violet"),
        ("#9932CC", "Dark Orchid"),
        ("#BA55D3", "Medium Orchid"),
    ],
    "pink": [
        ("#FF69B4", "Hot Pink"),
        ("#FFB6C1", "Light Pink"),
        ("#FFC0CB", "Pink"),
        ("#DB7093", "Pale Violet Red"),
        ("#FF1493", "Deep Pink"),
    ],
    "white": [
        ("#FFFFFF", "White"),
        ("#F8F8FF", "Ghost White"),
        ("#FFF5EE", "Seashell"),
        ("#FFFAF0", "Floral White"),
        ("#FFFFF0", "Ivory"),
    ],
    "gray": [
        ("#808080", "Gray"),
        ("#A9A9A9", "Dark Gray"),
        ("#C0C0C0", "Silver"),
        ("#D3D3D3", "Light Gray"),
        ("#696969", "Dim Gray"),
        ("#BEBEBE", "Very Light Gray"),
    ],
    "black": [
        ("#000000", "Black"),
        ("#1A1A1A", "Near Black"),
        ("#2F2F2F", "Dark Gray"),
    ],
}

_EMOJI_MAP: dict[str, str] = {}
for key, _, emoji in COLOR_HUES:
    _EMOJI_MAP[key] = emoji

_HEX_TO_HUE: dict[str, str] = {}
for hue, shades in _SHADES.items():
    for hex_val, _ in shades:
        _HEX_TO_HUE[hex_val.upper()] = hue

_HUE_LABELS: dict[str, str] = {key: name for key, name, _ in COLOR_HUES}
_SHADE_LABELS: dict[str, str] = {}
for shades in _SHADES.values():
    for hex_val, label in shades:
        _SHADE_LABELS[hex_val.upper()] = label


def color_emoji(value: str) -> str:
    v = value.strip().upper()
    hue = _HEX_TO_HUE.get(v)
    if hue:
        return _EMOJI_MAP.get(hue, "🎨")
    if v in _EMOJI_MAP:
        return _EMOJI_MAP[v]
    return "🎨"


def color_label(value: str) -> str:
    v = value.strip().upper()
    if v in _SHADE_LABELS:
        return _SHADE_LABELS[v]
    if v in _HUE_LABELS:
        return _HUE_LABELS[v]
    return value


def shade_options(hue: str) -> list[tuple[str, str]]:
    return _SHADES.get(hue, [("#FFFFFF", "White")])


def _get_hue_for_hex(hex_color: str) -> str | None:
    v = hex_color.strip().upper()
    return _HEX_TO_HUE.get(v)


_NAMED_RGB: dict[str, str] = {
    "white": "FFFFFF",
    "black": "000000",
    "red": "FF0000",
    "green": "00FF00",
    "blue": "0000FF",
    "yellow": "FFFF00",
    "cyan": "00FFFF",
    "magenta": "FF00FF",
    "orange": "FFA500",
    "purple": "800080",
    "pink": "FFC0CB",
    "gray": "808080",
}


def _normalize_rgb_hex(color: str, default: str = "FFFFFF") -> str:
    c = color.strip()
    if c.startswith("#"):
        c = c.lstrip("#")
        if len(c) == 6:
            return c.upper()
        if len(c) == 3:
            return "".join(ch * 2 for ch in c).upper()
        return default
    return _NAMED_RGB.get(c.lower(), default)


def resolve_ass_color(color: str) -> str:
    rgb = _normalize_rgb_hex(color)
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H00{b}{g}{r}"


def resolve_drawtext_color(value: str | None, default: str = "white") -> str:
    if not value:
        return f"#{_normalize_rgb_hex(default)}"
    return f"#{_normalize_rgb_hex(value, _normalize_rgb_hex(default))}"
