"""检查样式表、页面和脚本中的内联 CSS 与 SVG 样式属性。"""

import re
from html.parser import HTMLParser
from pathlib import Path

import tinycss2

STATIC = Path(__file__).resolve().parents[2] / "src" / "aiops" / "static"
TOKENS = {
    "--bg": "#0b0c0e",
    "--bg-sunk": "#08090a",
    "--surface": "#121417",
    "--surface-2": "#171a1e",
    "--chalk": "#ece8e1",
    "--ink-2": "#c9c5bc",
    "--muted": "#a49f96",
    "--dim": "#7e7a72",
    "--line": "rgba(236,232,225,.13)",
    "--line-soft": "rgba(236,232,225,.07)",
    "--line-hi": "rgba(236,232,225,.26)",
    "--glass": "rgba(8,9,10,.72)",
    "--gold": "#cca460",
    "--gold-hi": "#e2c891",
    "--gold-deep": "#7f6238",
    "--gold-wash": "rgba(204,164,96,.10)",
    "--ok": "#6e8f6b",
    "--warn": "#d9a13c",
    "--danger": "#c82a45",
    "--danger-wash": "rgba(200,42,69,.12)",
    "--fast": "180ms",
    "--click": "240ms",
    "--settle": "360ms",
    "--ui": "cubic-bezier(.2,.85,.25,1)",
    "--enter": "cubic-bezier(.16,.78,.22,1)",
    "--radius": "2px",
    "--gutter": "clamp(20px,2.6vw,48px)",
    "--display": '"notoserifdisplay","notoserifcjksc",georgia,serif',
    "--sans": 'inter,"notosanscjksc",system-ui,sans-serif',
    "--mono": '"dejavusansmono","notosansmonocjksc",monospace',
}
COLORS = {v for v in TOKENS.values() if v.startswith(("#", "rgba("))} | {
    "inherit",
    "transparent",
    "currentcolor",
    "none",
}
SIZES = {f"{n}px" for n in (10, 11, 12, 13, 14, 20, 28, 30, 34)} | {"inherit"}
FAMILIES = {TOKENS[key] for key in ("--display", "--sans", "--mono")} | {"inherit"}
TRANSITIONS = {
    "color",
    "background-color",
    "border-color",
    "outline-color",
    "grid-template-rows",
    "visibility",
}
SVG = {"fill", "stroke", "stroke-width"}
COMMON = re.compile(r"^var\((--[\w-]+)\)$")


def normalized(value):
    return re.sub(r"\s+", "", value).lower()


def resolved(value):
    token = COMMON.fullmatch(value)
    return TOKENS.get(token[1], value) if token else value


def components(value):
    return [
        resolved(normalized(tinycss2.serialize([token])))
        for token in value
        if token.type not in {"whitespace", "comment"}
    ]


def motion(name, items):
    groups = [[]]
    for item in items:
        if item == ",":
            groups.append([])
        else:
            groups[-1].append(item)
    for group in groups:
        if group == ["none"]:
            continue
        if name == "transition":
            assert len(group) == 3
            prop, duration, timing = group
            assert prop in TRANSITIONS and timing == TOKENS["--ui"]
            assert duration == (
                "240ms" if prop in {"grid-template-rows", "visibility"} else "180ms"
            )
        else:
            assert group in (
                ["spin", "1.6s", "linear", "infinite"],
                ["enter", "360ms", TOKENS["--enter"], "both"],
                ["status-change", "240ms", TOKENS["--ui"], "both"],
            )


def check_property(name, value, selector="", *, svg=False):
    raw = normalized(tinycss2.serialize(value))
    actual = resolved(raw)
    if name in TOKENS:
        assert raw == TOKENS[name], (name, raw)
    if name in {"background", "background-image", "box-shadow", "text-shadow"}:
        assert actual == "none" or (name == "background" and actual in COLORS)
    if name in {"color", "background-color", "fill", "stroke"}:
        assert actual in COLORS, (selector, name, raw)
    elif name == "border-radius":
        assert actual in {"0", "2px"}, (selector, name, raw)
    elif name == "stroke-width":
        assert actual in ({"0", "1px", "1"} if svg else {"0", "1px"})
    elif re.fullmatch(
        r"(border(?:-(?:top|bottom|left|right|block(?:-start|-end)?|inline(?:-start|-end)?))?"
        r"|outline)(-(color|width|style))?",
        name,
    ):
        allowed = COLORS | {"0", "1px", "solid"}
        if selector == ".session-row.selected" and name == "border-left":
            allowed |= {"2px"}
        assert set(components(value)) <= allowed, (selector, name, raw)
    elif name == "outline-offset":
        assert actual in {"0", "1px", "-1px", "2px"}
    elif name == "border-collapse":
        assert actual in {"collapse", "separate"}
    elif name.startswith(("border-", "outline-")):
        raise AssertionError(f"未检查的描边属性 {name}")
    elif name == "font":
        assert actual == "inherit"
    elif name == "font-family":
        assert actual in FAMILIES
    elif name == "font-size":
        assert actual in SIZES
    elif name == "font-weight":
        assert actual in {"400", "inherit"}
    elif name == "line-height" and selector in {"body", ".compose-row textarea"}:
        assert actual == "1.65"
    elif name in {"transition", "animation"}:
        motion(name, components(value))
    elif name.startswith(("transition-", "animation-")):
        raise AssertionError(f"请用可检查的完整声明 {name}")
    elif name == "backdrop-filter":
        assert selector == ".event-popover" and actual == "blur(12px)saturate(120%)"


def check_declarations(source, selector=""):
    for entry in tinycss2.parse_declaration_list(source, skip_comments=True, skip_whitespace=True):
        assert entry.type == "declaration", (selector, entry)
        check_property(entry.lower_name, entry.value, selector)


def check_rules(rules):
    for rule in rules:
        if rule.type == "qualified-rule":
            check_declarations(rule.content, tinycss2.serialize(rule.prelude).strip())
        else:
            assert rule.type == "at-rule" and rule.lower_at_keyword in {"media", "keyframes"}, rule
            check_rules(
                tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True)
            )


class PageStyles(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        assert tag not in {"iframe", "object", "embed"}
        self.in_style = tag == "style"
        for name, val in attrs:
            if name == "style":
                check_declarations(val, tag)
            elif name in SVG:
                check_property(name, tinycss2.parse_component_value_list(val), tag, svg=True)

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            check_rules(tinycss2.parse_stylesheet(data, skip_comments=True, skip_whitespace=True))


def check_script(source):
    # 动态样式只允许账本的几何定位，颜色、字体和 SVG 描边由样式表管理。
    assignments = re.findall(r"\.style\.(\w+)\s*=", source)
    assert set(assignments) <= {"left", "top", "maxWidth"}
    properties = re.findall(r"\.style\.setProperty\(\s*['\"]([^'\"]+)", source)
    assert set(properties) <= {"--ledger-width"}
    assert not re.search(r"\.style\s*=|\.style\.cssText\s*=|\.insertRule\(", source)
    for match in re.finditer(
        r"""\.setAttribute\(\s*["'](style|fill|stroke|stroke-width)["']\s*,\s*([^\n]+?)\)""", source
    ):
        name, literal = match.groups()
        assert literal[0] in {"'", '"'} and literal[-1] == literal[0], literal
        if name == "style":
            check_declarations(literal[1:-1], "JS 内联样式")
        else:
            check_property(name, tinycss2.parse_component_value_list(literal[1:-1]), svg=True)


def main():
    for path in STATIC.glob("*.css"):
        rules = tinycss2.parse_stylesheet(
            path.read_text(), skip_comments=True, skip_whitespace=True
        )
        check_rules(rules)
    for path in STATIC.glob("*.html"):
        PageStyles().feed(path.read_text())
    for path in STATIC.glob("*.js"):
        check_script(path.read_text())
    print("网页样式静态检查通过")


if __name__ == "__main__":
    main()
