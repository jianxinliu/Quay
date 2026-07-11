"""内置示例：首次启动时播种到 workflow 库，展示 DAG 画布的完整能力。

示例「渠道ROI分析」覆盖全部七类节点：
  两个取数（orders / users，同库不同查询）+ 一个文件源（渠道投放成本 CSV）
  → 过滤（只留已支付）→ JOIN（订单×用户）→ 聚合（按渠道汇总收入）
  → 自由 SQL（联成本表算 ROI）→ 输出（按 ROI 排序），结果默认以柱状图呈现。

source 节点引用连接 local/demo-mysql（dbm_e2e 库的 orders/users 表）；
换环境使用时在画布上点开取数节点改成自己的连接即可——模板价值在于结构。
"""

from __future__ import annotations

from pathlib import Path

EXAMPLE_NAME = "示例 · 渠道ROI分析"
EXAMPLE_WORKSPACE = "ws1"
EXAMPLE_CSV_REL = "demo/channel_cost.csv"  # 相对 data 目录

EXAMPLE_CSV = """channel,cost_total
douyin,3500
wechat,1800
web,600
appstore,2600
"""

EXAMPLE_CHART = {"view": "chart", "type": "bar", "x": "channel", "y": "revenue", "agg": ""}


def example_graph(csv_path: str) -> dict:
    return {
        "nodes": [
            {"id": "n_src_orders", "type": "source", "name": "orders", "x": 30, "y": 40,
             "cfg": {"conn": "local/demo-mysql", "sql": "SELECT * FROM orders",
                     "limit": 100000}},
            {"id": "n_src_users", "type": "source", "name": "users", "x": 30, "y": 150,
             "cfg": {"conn": "local/demo-mysql",
                     "sql": "SELECT id, name, active FROM users", "limit": 10000}},
            {"id": "n_cost", "type": "file", "name": "channel_cost", "x": 30, "y": 260,
             "cfg": {"path": csv_path}},
            {"id": "n_paid", "type": "filter", "name": "paid_orders", "x": 260, "y": 40,
             "cfg": {"where": "status = 'paid'"}},
            {"id": "n_join", "type": "join", "name": "orders_with_user", "x": 490, "y": 90,
             "cfg": {"kind": "INNER", "on": "l.uid = r.id",
                     "select": "l.*, r.name AS user_name, r.active"}},
            {"id": "n_agg", "type": "aggregate", "name": "by_channel", "x": 720, "y": 90,
             "cfg": {"group": "channel",
                     "aggs": "count(*) AS orders_n, round(sum(amount), 2) AS revenue,"
                             " round(avg(amount), 2) AS avg_amount,"
                             " count(DISTINCT uid) AS buyers"}},
            {"id": "n_roi", "type": "sql", "name": "channel_roi", "x": 950, "y": 150,
             "cfg": {"sql": "SELECT b.channel, b.orders_n, b.buyers, b.revenue,"
                            " b.avg_amount, c.cost_total,"
                            " round(b.revenue / c.cost_total, 2) AS roi"
                            " FROM by_channel b JOIN channel_cost c"
                            " ON b.channel = c.channel"}},
            {"id": "n_out", "type": "output", "name": "report", "x": 1180, "y": 150,
             "cfg": {"order_by": "roi DESC", "limit": 100}},
        ],
        "edges": [
            {"from": "n_src_orders", "to": "n_paid", "port": "in"},
            {"from": "n_paid", "to": "n_join", "port": "left"},
            {"from": "n_src_users", "to": "n_join", "port": "right"},
            {"from": "n_join", "to": "n_agg", "port": "in"},
            {"from": "n_agg", "to": "n_roi", "port": "in"},
            {"from": "n_cost", "to": "n_roi", "port": "in2"},  # 仅表意+定序（SQL 节点按名引用）
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
