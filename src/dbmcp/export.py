"""查询结果导出：CSV / JSON / Markdown / Excel。

纯函数，输入 columns + rows（rows 单元格是 engines._jsonable 产出的标量，
或 {"__bytes_base64__": ...} 包装的二进制），输出 (bytes, media_type, ext)。
便于单测，与传输层解耦。openpyxl 惰性导入——未安装也不影响本模块加载。
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from typing import Any


class ExportError(Exception):
    """导出失败（格式不支持 / 缺依赖）。message 面向使用者。"""


def _text_cell(v: Any) -> str:
    """把单元格值渲染成纯文本（CSV/Markdown 用）。"""
    if v is None:
        return ""
    if isinstance(v, dict) and "__bytes_base64__" in v:
        return "base64:" + str(v["__bytes_base64__"])
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def to_csv(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(list(columns))
    for row in rows:
        writer.writerow([_text_cell(v) for v in row])
    # BOM：让 Excel 双击打开时正确识别 UTF-8（中文不乱码）
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def to_json(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    data = [dict(zip(columns, row, strict=False)) for row in rows]
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def to_markdown(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    def esc(v: Any) -> str:
        return _text_cell(v).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(esc(c) for c in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines.extend("| " + " | ".join(esc(v) for v in row) + " |" for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def to_xlsx(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    try:
        from openpyxl import Workbook  # noqa: PLC0415
    except ImportError as e:  # 惰性依赖：仅 xlsx 导出需要
        raise ExportError("导出 Excel 需要 openpyxl，请运行 `uv sync` 安装后重试") from e

    wb = Workbook()
    ws = wb.active
    ws.append(list(columns))
    for row in rows:
        # openpyxl 只接受标量/日期；dict（二进制）等复杂值转文本
        ws.append([v if isinstance(v, (int, float, bool, str)) or v is None
                   else _text_cell(v) for v in row])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# 格式 → (media_type, 文件扩展名, 序列化函数)
_EXPORTERS = {
    "csv": ("text/csv; charset=utf-8", "csv", to_csv),
    "json": ("application/json; charset=utf-8", "json", to_json),
    "markdown": ("text/markdown; charset=utf-8", "md", to_markdown),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
        to_xlsx,
    ),
}

SUPPORTED_FORMATS = tuple(_EXPORTERS)


def export_result(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], fmt: str
) -> tuple[bytes, str, str]:
    """按格式序列化，返回 (内容字节, media_type, 扩展名)。"""
    entry = _EXPORTERS.get(fmt)
    if entry is None:
        raise ExportError(f"不支持的导出格式 {fmt!r}，可选：{', '.join(SUPPORTED_FORMATS)}")
    media_type, ext, fn = entry
    return fn(columns, rows), media_type, ext
