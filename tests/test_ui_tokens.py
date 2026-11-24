"""界面规范守护：内容页 CSS 不允许裸色值（深浅主题只换 admin-chrome.css 里的变量）。"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "dbmcp" / "static"
# 只匹配属性值里的十六进制色（冒号之后），避开 #id 选择器
_HEX_IN_VALUE = re.compile(r"\{[^}]*#[0-9a-fA-F]{3,8}\b[^}]*\}")


@pytest.mark.parametrize("name", ["settings.css", "dashboard.css"])
def test_no_bare_hex_colors(name: str) -> None:
    css = re.sub(r"/\*.*?\*/", "", (STATIC / name).read_text(), flags=re.S)
    bad = [m.group(0) for m in _HEX_IN_VALUE.finditer(css)]
    assert not bad, f"{name} 出现裸色值，请改用 admin-chrome.css 中的变量：\n" + "\n".join(bad)


def test_reduced_motion_covers_animation() -> None:
    css = (STATIC / "admin-chrome.css").read_text()
    assert "animation:none!important" in css


def test_console_accent_matches_admin() -> None:
    css = (STATIC / "console.css").read_text()
    assert "#3574f0" not in css and "--dg-accent: #0d9488" in css
