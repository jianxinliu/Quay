"""内置示例 workflow 端到端：用真实 demo 示例库跑完整 DAG，确认产出 ROI 结果。

复现 service._run_plan 的执行：连接源 SQL 打到真实 SQLite demo 库取数导入 DuckDB 工作区，
文件源读 CSV，再顺序执行各节点 VIEW，最后跑输出 SQL。防止示例模板引用了不存在的
连接/表/列（旧模板曾引用不存在的 local/demo-mysql 与 users 表）。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from dbmcp.analysis import AnalysisStore  # noqa: E402
from dbmcp.examples import EXAMPLE_CSV, example_graph  # noqa: E402
from dbmcp.workflows import compile_graph  # noqa: E402

_SEED = Path(__file__).resolve().parent.parent / "scripts" / "demo_seed.py"
_spec = importlib.util.spec_from_file_location("demo_seed", _SEED)
demo_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_seed)


def test_example_workflow_runs_on_demo_db(tmp_path):
    db_path = tmp_path / "demo.sqlite3"
    demo_seed.build(str(db_path))
    csv_path = tmp_path / "category_cost.csv"
    csv_path.write_text(EXAMPLE_CSV, encoding="utf-8")

    plan = compile_graph(example_graph(str(csv_path)))
    store = AnalysisStore(tmp_path / "analysis")
    src_conn = sqlite3.connect(str(db_path))

    # 1) 导入所有数据源：先连接源（import_rows 会自动创建工作区），再文件源
    conn_sources = [s for s in plan["sources"] if s["kind"] != "file"]
    file_sources = [s for s in plan["sources"] if s["kind"] == "file"]
    for src in conn_sources:
        cur = src_conn.execute(src["sql"])
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        store.import_rows("ws1", src["dataset"], cols, rows)
    for src in file_sources:
        store.run_sql("ws1", f"CREATE OR REPLACE TABLE {src['dataset']} AS "
                             f"SELECT * FROM read_csv_auto('{src['path']}')")

    # 2) 顺序执行各节点 VIEW（valid_orders → … → category_roi）
    for st in plan["steps"]:
        store.run_sql("ws1", st["sql"])

    # 3) 输出 SQL
    out = store.run_sql("ws1", plan["output_sql"])
    cols = out["columns"]
    rows = out["rows"]

    # 结果结构：分类 + 销售额 + ROI，按 ROI 降序
    assert "category" in cols and "revenue" in cols and "roi" in cols
    assert rows, "示例 workflow 输出为空"
    # demo 有 7 个分类，取消订单可能让个别分类无销售，至少应有多行
    assert len(rows) >= 5
    roi_idx = cols.index("roi")
    rois = [r[roi_idx] for r in rows]
    assert rois == sorted(rois, reverse=True)  # 按 ROI 降序
    assert all(isinstance(v, (int, float)) and v > 0 for v in rois)
    src_conn.close()
