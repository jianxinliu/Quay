"""给 agent 的 TSV 输出渲染 + 字符预算裁剪测试（纯函数）。"""

from dbmcp.agent_format import render_agent_result


def _parse(text: str):
    lines = text.split("\n")
    meta = {}
    body = []
    types = None
    for ln in lines:
        if ln.startswith("# types:"):
            types = ln[len("# types:"):].strip()
        elif ln.startswith("# note:"):
            continue
        elif ln.startswith("#"):
            for kv in ln[1:].split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    meta[k] = v
        else:
            body.append(ln)
    return meta, types, body


def test_basic_tsv_header_and_null():
    result = {"columns": ["id", "name"], "rows": [["1", "alice"], ["2", None]],
              "column_types": ["number", "string"], "duration_ms": 5, "statement_kind": "Select"}
    out = render_agent_result(result, char_budget=10000)
    meta, types, body = _parse(out)
    assert meta["shown"] == "2" and meta["truncated"] == "false"
    assert types == "number, string"
    assert body[0] == "id\tname"          # 列名首行
    assert body[1] == "1\talice"
    assert body[2] == "2\t\\N"            # NULL → \N


def test_escaping_tab_newline_backslash():
    result = {"columns": ["c"], "rows": [["a\tb"], ["x\ny"], ["p\\q"]]}
    out = render_agent_result(result, char_budget=10000)
    _, _, body = _parse(out)
    assert body[1] == "a\\tb"    # tab 转义
    assert body[2] == "x\\ny"    # newline 转义（保证一行一条记录）
    assert body[3] == "p\\\\q"   # 反斜杠转义


def test_bool_and_dict_values():
    result = {"columns": ["flag", "j"], "rows": [[True, {"a": 1}], [False, [1, 2]]]}
    out = render_agent_result(result, char_budget=10000)
    _, _, body = _parse(out)
    assert body[1] == 'true\t{"a":1}'
    assert body[2] == "false\t[1,2]"


def test_char_budget_truncation():
    rows = [[f"row-{i}", "x" * 50] for i in range(200)]
    result = {"columns": ["a", "b"], "rows": rows, "truncated": False}
    out = render_agent_result(result, char_budget=800)
    meta, _, body = _parse(out)
    shown = int(meta["shown"])
    assert shown < 200                       # 被预算截断
    assert meta["truncated"] == "true" and meta["reason"] == "char_budget"
    assert len(body) == shown + 1            # +1 是列名行
    assert "note" in out                     # 有引导提示
    assert len(out) <= 800 + 200             # 大致不超预算（+预留）


def test_db_row_cap_truncation_reason():
    result = {"columns": ["a"], "rows": [["1"], ["2"]], "truncated": True}
    out = render_agent_result(result, char_budget=10000)
    meta, _, _ = _parse(out)
    assert meta["truncated"] == "true" and meta["reason"] == "row_cap"


def test_empty_rows():
    result = {"columns": ["a", "b"], "rows": [], "column_types": ["number", "string"]}
    out = render_agent_result(result, char_budget=10000)
    meta, _, body = _parse(out)
    assert meta["shown"] == "0" and meta["truncated"] == "false"
    assert body == ["a\tb"]                   # 只有列名行


def test_at_least_one_row_even_if_over_budget():
    # 单行就超预算：仍给 1 行（否则 agent 拿不到任何数据）
    result = {"columns": ["a"], "rows": [["x" * 500], ["y" * 500]]}
    out = render_agent_result(result, char_budget=50)
    meta, _, body = _parse(out)
    assert int(meta["shown"]) == 1
