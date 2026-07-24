"""内置示例：首次启动时播种到 workflow 库，展示 DAG 画布的完整能力。

示例「分类销售 ROI 分析」基于内置 SQLite 示例库（连接 demo/demo-sqlite），覆盖全部七类节点：
  三个取数（order_items 明细 / products 带分类 / orders 订单头）+ 一个文件源（各分类投放成本 CSV）
  → 过滤（剔除已取消订单）→ JOIN×2（明细×有效订单、明细×产品分类）
  → 聚合（按分类汇总销售额）→ 自由 SQL（联成本算 ROI）→ 输出（按 ROI 排序），默认柱状图。

source 节点引用连接 demo/demo-sqlite（首次启动自带的示例库）；换到自己的库时，
在画布上点开取数节点改连接与 SQL 即可——模板价值在于结构。
"""

from __future__ import annotations

from pathlib import Path

EXAMPLE_NAME = "示例 · 分类销售 ROI 分析"
EXAMPLE_WORKSPACE = "ws1"
EXAMPLE_CSV_REL = "demo/category_cost.csv"  # 相对 data 目录

# 各分类的市场投放成本（category 需与示例库 categories.name 一致）
EXAMPLE_CSV = """category,cost_total
电脑外设,8000
显示设备,6000
音频设备,5000
存储设备,3000
办公家具,7000
线缆配件,2000
网络设备,4000
"""

EXAMPLE_CHART = {"view": "chart", "type": "bar", "x": "category", "y": "revenue", "agg": ""}


def example_graph(csv_path: str) -> dict:
    return {
        "nodes": [
            {"id": "n_src_items", "type": "source", "name": "items", "x": 30, "y": 40,
             "cfg": {"conn": "demo/demo-sqlite",
                     "sql": "SELECT order_id, product_id, qty, unit_price, discount"
                            " FROM order_items",
                     "limit": 100000}},
            {"id": "n_src_prod", "type": "source", "name": "products_cat", "x": 30, "y": 150,
             "cfg": {"conn": "demo/demo-sqlite",
                     "sql": "SELECT p.id AS product_id, c.name AS category"
                            " FROM products p JOIN categories c ON p.category_id = c.id",
                     "limit": 10000}},
            {"id": "n_src_orders", "type": "source", "name": "orders", "x": 30, "y": 260,
             "cfg": {"conn": "demo/demo-sqlite",
                     "sql": "SELECT id AS order_id, status FROM orders",
                     "limit": 100000}},
            {"id": "n_cost", "type": "file", "name": "category_cost", "x": 30, "y": 370,
             "cfg": {"path": csv_path}},
            {"id": "n_valid", "type": "filter", "name": "valid_orders", "x": 260, "y": 260,
             "cfg": {"where": "status <> '已取消'"}},
            {"id": "n_join_io", "type": "join", "name": "valid_items", "x": 490, "y": 130,
             "cfg": {"kind": "INNER", "on": "l.order_id = r.order_id",
                     "select": "l.*"}},
            {"id": "n_join_ip", "type": "join", "name": "items_cat", "x": 720, "y": 130,
             "cfg": {"kind": "INNER", "on": "l.product_id = r.product_id",
                     "select": "l.*, r.category"}},
            {"id": "n_agg", "type": "aggregate", "name": "by_category", "x": 950, "y": 130,
             "cfg": {"group": "category",
                     "aggs": "count(DISTINCT order_id) AS orders_n, count(*) AS items_n,"
                             " round(sum(qty * unit_price * (1 - discount)), 2) AS revenue,"
                             " round(avg(unit_price), 2) AS avg_price"}},
            {"id": "n_roi", "type": "sql", "name": "category_roi", "x": 1180, "y": 190,
             "cfg": {"sql": "SELECT b.category, b.orders_n, b.items_n, b.revenue,"
                            " c.cost_total,"
                            " round(b.revenue / c.cost_total, 2) AS roi"
                            " FROM by_category b JOIN category_cost c"
                            " ON b.category = c.category"}},
            {"id": "n_out", "type": "output", "name": "report", "x": 1410, "y": 190,
             "cfg": {"order_by": "roi DESC", "limit": 100}},
        ],
        "edges": [
            {"from": "n_src_orders", "to": "n_valid", "port": "in"},
            {"from": "n_src_items", "to": "n_join_io", "port": "left"},
            {"from": "n_valid", "to": "n_join_io", "port": "right"},
            {"from": "n_join_io", "to": "n_join_ip", "port": "left"},
            {"from": "n_src_prod", "to": "n_join_ip", "port": "right"},
            {"from": "n_join_ip", "to": "n_agg", "port": "in"},
            {"from": "n_agg", "to": "n_roi", "port": "in"},
            {"from": "n_cost", "to": "n_roi", "port": "in2"},  # 表意+定序（SQL 节点按名引用）
            {"from": "n_roi", "to": "n_out", "port": "in"},
        ],
    }


def seed_examples(workflows, data_dir: str | Path) -> bool:
    """workflow 表为空时写入内置示例（返回是否播种）。

    以「表为空」为条件而非按名判断：用户删除示例后重启不会复活
    （除非删光了所有 workflow）。示例 CSV 落到 data 目录（已存在则不覆盖）。
    """
    if workflows.list():
        return False
    csv_path = Path(data_dir) / EXAMPLE_CSV_REL
    if not csv_path.exists():
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(EXAMPLE_CSV, encoding="utf-8")
    graph = example_graph(str(csv_path))
    from .workflows import compile_graph
    sources = compile_graph(graph)["sources"]  # 顺带校验模板本身合法
    workflows.save(EXAMPLE_NAME, EXAMPLE_WORKSPACE, "", sources,
                   chart=EXAMPLE_CHART, graph=graph)
    return True
