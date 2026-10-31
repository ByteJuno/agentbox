"""个人主题：沿用上游令牌白名单和安全 CSS 取值语法，不接受任意 CSS。

令牌表来源 internal/theme/manifest.go (e19df41)；更新前端时一并核对。
"""
import re
import unicodedata

BASES = ["amber", "glass", "cyberpunk", "graphite", "verdant", "blueprint"]
TOKENS = {'--bg': 'color', '--panel': 'color', '--panel-2': 'color', '--panel-solid': 'color', '--line': 'color', '--line-soft': 'color', '--text': 'color', '--text-hi': 'color', '--muted': 'color', '--scroll-thumb': 'color', '--scroll-thumb-hover': 'color', '--scroll-thumb-active': 'color', '--amber': 'color', '--amber-dim': 'color', '--accent': 'color', '--accent-hi': 'color', '--on-accent': 'color', '--green': 'color', '--green-line': 'color', '--red': 'color', '--red-hi': 'color', '--red-line': 'color', '--on-red': 'color', '--warn': 'color', '--warn-line': 'color', '--code-bg': 'color', '--code-head': 'color', '--code-line': 'color', '--syntax-keyword': 'color', '--syntax-function': 'color', '--term-bg': 'color', '--term-fg': 'color', '--term-cursor': 'color', '--term-sel': 'color', '--scrim': 'color', '--backdrop': 'color', '--backdrop-strong': 'color', '--overlay-chip': 'color', '--shadow-sm': 'shadow', '--shadow-md': 'shadow', '--shadow-lg': 'shadow', '--checker-a': 'color', '--checker-b': 'color', '--diff-add': 'color', '--info': 'color', '--busy-veil': 'color', '--field': 'color', '--page': 'color', '--app-canvas': 'canvas', '--skin-detail': 'color', '--glass-glint': 'color', '--glass-edge': 'color', '--radius-scale': 'scale', '--radius-pill': 'radius', '--radius-round': 'radius', '--sans': 'font', '--mono': 'font'}
COLOR_FUNCS = {"rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color", "color-mix"}
GRADIENTS = {"linear-gradient", "radial-gradient", "conic-gradient", "repeating-linear-gradient",
             "repeating-radial-gradient", "repeating-conic-gradient"}


class InvalidTheme(ValueError):
    def __init__(self, code, token="", section=""):
        super().__init__("主题清单无效：" + code)
        self.problem = {"code": code, **({"token": token} if token else {}), **({"section": section} if section else {})}


def safe_value(kind, value):
    if not isinstance(value, str):
        raise ValueError
    value = " ".join(value.split())
    if not value:
        raise ValueError
    number = r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
    if kind == "scale":
        if not re.fullmatch(number, value) or float(value) > 3:
            raise ValueError
    elif kind == "radius":
        if not re.fullmatch(number + r"(?:px|%)", value) or float(value.rstrip("px%")) > 9999:
            raise ValueError
    elif kind == "font":
        parts = value.split(",")
        if len(value) > 300 or len(parts) > 12:
            raise ValueError
        for part in parts:
            part = part.strip()
            if part.startswith('"') and part.endswith('"'):
                name = part[1:-1]
                if not name or not all(c.isalnum() or c in " ._-" for c in name):
                    raise ValueError
            elif not part or not all(re.fullmatch(r"-?[A-Za-z_][A-Za-z0-9_-]*", w) for w in part.split()):
                raise ValueError
        value = ", ".join(p.strip() for p in parts)
    else:
        if len(value) > (1200 if kind == "canvas" else 200) or not re.fullmatch(r"[A-Za-z0-9 #%.,()/+_-]+", value):
            raise ValueError
        # var 只引用白名单，不允许 fallback、转义或外部资源函数。
        def variable(match):
            if match[1].strip() not in TOKENS:
                raise ValueError
            return "red"
        check = re.sub(r"var\(([^()]*)\)", variable, value, flags=re.I)
        allowed = COLOR_FUNCS | (GRADIENTS if kind == "canvas" else set())
        depth, i = 0, 0
        while i < len(check):
            word = re.match(r"[-A-Za-z0-9_+%.]+", check[i:])
            if word:
                end = i + len(word[0])
                if end < len(check) and check[end] == "(":
                    if word[0].lower() not in allowed:
                        raise ValueError
                    depth += 1
                    if depth > 6:
                        raise ValueError
                    end += 1
                i = end
            elif check[i] == "#":
                hexa = re.match(r"#[A-Za-z0-9]+", check[i:])
                if not hexa or not re.fullmatch(r"#(?:[a-fA-F0-9]{3,4}|[a-fA-F0-9]{6}|[a-fA-F0-9]{8})", hexa[0]):
                    raise ValueError
                i += len(hexa[0])
            elif check[i] == ")":
                depth -= 1
                if depth < 0:
                    raise ValueError
                i += 1
            elif check[i] == "(":
                raise ValueError
            else:
                i += 1
        if depth:
            raise ValueError
    return value


def validate(manifest):
    if not isinstance(manifest, dict):
        raise InvalidTheme("bad_json")
    unknown = set(manifest) - {"agentbox_theme", "id", "name", "base", "author", "description", "common", "dark", "light"}
    if unknown:
        raise InvalidTheme("unknown_field", sorted(unknown)[0])
    if type(manifest.get("agentbox_theme")) is not int or manifest["agentbox_theme"] != 1:
        raise InvalidTheme("format")
    if not isinstance(manifest.get("id"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", manifest["id"]):
        raise InvalidTheme("id")
    if manifest.get("base") not in BASES:
        raise InvalidTheme("base")
    result = dict(manifest)
    for key, limit in (("name", 40), ("author", 60), ("description", 200)):
        value = manifest.get(key, "")
        if not isinstance(value, str) or len(value.strip()) > limit or any(unicodedata.category(c) == "Cc" or c in "\u2028\u2029" for c in value):
            raise InvalidTheme(key)
        result[key] = value.strip()
    if not result["name"]:
        raise InvalidTheme("name")
    for section in ("common", "dark", "light"):
        group = manifest.get(section) or {}
        if not isinstance(group, dict):
            raise InvalidTheme("bad_type", section)
        result[section] = {}
        for token, value in group.items():
            if token not in TOKENS:
                raise InvalidTheme("unknown_token", token, section)
            try:
                result[section][token] = safe_value(TOKENS[token], value)
            except ValueError:
                raise InvalidTheme("bad_value", token, section)
    return result
