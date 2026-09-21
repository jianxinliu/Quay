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


def _md_escape(v: Any) -> str:
    """Markdown 单元格转义：反斜杠、管道符、换行。流式与非流式 writer 共用，避免两处走样。"""
    return _text_cell(v).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


class StreamWriter:
    """流式导出骨架：行逐条喂进来，结束时一次性给出字节。

    导出完整结果集（可能上百万行）时，把所有行先收成列表再序列化会把内存翻倍；
    改成每行到手就写进缓冲，任意时刻只持有序列化缓冲（bytes，比 Python 对象列表
    紧凑一个数量级）。to_* 旧函数保留给非流式调用方（如 agent 的 export_table）。
    """

    def write(self, row: Sequence[Any]) -> None:  # noqa: B027
        raise NotImplementedError

    def bytes(self) -> bytes:  # noqa: B027
        raise NotImplementedError


class _CsvWriter(StreamWriter):
    def __init__(self, columns: Sequence[str]) -> None:
        self._buf = io.StringIO()
        self._w = csv.writer(self._buf)
        self._w.writerow(list(columns))

    def write(self, row: Sequence[Any]) -> None:
        self._w.writerow([_text_cell(v) for v in row])

    def bytes(self) -> bytes:
        # BOM：让 Excel 双击打开时正确识别 UTF-8（中文不乱码）
        return b"\xef\xbb\xbf" + self._buf.getvalue().encode("utf-8")


class _JsonWriter(StreamWriter):
    def __init__(self, columns: Sequence[str]) -> None:
        self._buf = io.StringIO()
        self._buf.write("[\n")
        self._cols = list(columns)
        self._first = True

    def write(self, row: Sequence[Any]) -> None:
        obj = dict(zip(self._cols, row, strict=False))
        # 与 to_json 的 indent=2 逐字节对齐：单个对象缩进 2 格后整体再缩进 2 格。
        # json.dumps 会把字符串里的换义成 \n，故输出里真正的换行都是结构性的，可安全替换。
        item = json.dumps(obj, ensure_ascii=False, indent=2).replace("\n", "\n  ")
        self._buf.write(("" if self._first else ",\n") + "  " + item)
        self._first = False

    def bytes(self) -> bytes:
        self._buf.write("\n]")
        return self._buf.getvalue().encode("utf-8")


class _MarkdownWriter(StreamWriter):
    def __init__(self, columns: Sequence[str]) -> None:
        self._lines = [
            "| " + " | ".join(str(c) for c in columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
        ]

    def write(self, row: Sequence[Any]) -> None:
        self._lines.append("| " + " | ".join(_md_escape(v) for v in row) + " |")

    def bytes(self) -> bytes:
        return ("\n".join(self._lines) + "\n").encode("utf-8")


class _XlsxWriter(StreamWriter):
    def __init__(self, columns: Sequence[str]) -> None:
        try:
            from openpyxl import Workbook  # noqa: PLC0415
        except ImportError as e:  # 惰性依赖：仅 xlsx 导出需要
            raise ExportError("导出 Excel 需要 openpyxl，请运行 `uv sync` 安装后重试") from e
        self._wb = Workbook()
        ws = self._wb.active
        ws.append(list(columns))

    def write(self, row: Sequence[Any]) -> None:
        # openpyxl 只接受标量/日期；dict（二进制）等复杂值转文本
        self._wb.active.append([
            v if isinstance(v, (int, float, bool, str)) or v is None else _text_cell(v)
            for v in row
        ])

    def bytes(self) -> bytes:
        buf = io.BytesIO()
        self._wb.save(buf)
        return buf.getvalue()


def stream_export(columns: Sequence[str], fmt: str) -> tuple[StreamWriter, str, str]:
    """建一个流式 writer：逐行 write()，最后 bytes() 收尾。返回 (writer, media_type, ext)。"""
    entry = _EXPORTERS.get(fmt)
    if entry is None:
        raise ExportError(f"不支持的导出格式 {fmt!r}，可选：{', '.join(SUPPORTED_FORMATS)}")
    media_type, ext, writer_cls = entry
    return writer_cls(columns), media_type, ext


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
        return _md_escape(v)

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
    "csv": ("text/csv; charset=utf-8", "csv", _CsvWriter),
    "json": ("application/json; charset=utf-8", "json", _JsonWriter),
    "markdown": ("text/markdown; charset=utf-8", "md", _MarkdownWriter),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
        _XlsxWriter,
    ),
}

SUPPORTED_FORMATS = tuple(_EXPORTERS)


def export_result(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], fmt: str
) -> tuple[bytes, str, str]:
    """按格式序列化，返回 (内容字节, media_type, 扩展名)。

    行数较少时（agent 单表导出、分析工作区）用这个一次性版本；查询台的完整结果集
    导出走 stream_export 流式写盘。
    """
    writer, media_type, ext = stream_export(columns, fmt)
    for row in rows:
        writer.write(row)
    return writer.bytes(), media_type, ext
