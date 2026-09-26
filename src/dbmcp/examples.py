"""内置示例：首次启动播种一个示例库与一条示例流程，让新装实例开箱就有东西可点。

- **示例库** `data/demo/shop.sqlite3`（customers / orders 两张表，两百来条订单，
  固定随机种子、每次生成内容相同）——示例配置里的 `demo/shop` 连接指向它，
  README 首屏截图跑的就是这个库。
- **示例流程**「渠道ROI分析」覆盖全部七类节点：
  两个取数（orders / users，同库不同查询）+ 一个文件源（渠道投放成本 CSV）
  → 过滤（只留已支付）→ JOIN（订单×用户）→ 聚合（按渠道汇总收入）
  → 自由 SQL（联成本表算 ROI）→ 输出（按 ROI 排序），结果默认以柱状图呈现。

取数节点引用连接 `demo/shop`；换环境使用时在画布上点开取数节点改成自己的连接即可——
模板价值在于结构。
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

EXAMPLE_NAME = "示例 · 渠道ROI分析"
EXAMPLE_WORKSPACE = "ws1"
EXAMPLE_CONN = "demo/shop"                 # 与 config/connections.example.yaml 里的示例连接一致
EXAMPLE_CSV_REL = "demo/channel_cost.csv"  # 相对 data 目录
DEMO_DB_REL = "demo/shop.sqlite3"          # 相对 data 目录

# 渠道集合要与示例库 orders.channel 一致，否则 ROI 那步 JOIN 出来是空表
CHANNELS = ("social", "paid_search", "referral", "email", "organic")
EXAMPLE_CSV = "channel,cost_total\n" + "".join(
    f"{ch},{cost}\n" for ch, cost in zip(CHANNELS, (3200, 2600, 900, 700, 400)))

EXAMPLE_CHART = {"view": "chart", "type": "bar", "x": "channel", "y": "revenue", "agg": ""}

_DEMO_SEED = 20260716
_DEMO_CUSTOMERS = 40
_DEMO_ORDERS = 200
_CITIES = ("Shanghai", "Beijing", "Shenzhen", "Hangzhou", "Chengdu", "Wuhan")


def _demo_rows(seed: int = _DEMO_SEED) -> tuple[list[tuple], list[tuple]]:
    """确定性生成客户与订单行：同一个种子永远得到同一份数据，示例截图/文档才对得上。"""
    rnd = random.Random(seed)
    customers = [
        (i, f"user{i:03d}", rnd.choice(_CITIES), 1 if rnd.random() < 0.85 else 0,
         f"2026-0{rnd.randint(1, 6)}-{rnd.randint(1, 28):02d}")
        for i in range(1, _DEMO_CUSTOMERS + 1)
    ]
    # 渠道权重：社交与付费搜索订单多，自然流量少——让柱状图有明显高低
    weights = (34, 28, 14, 14, 10)
    statuses = ("paid",) * 8 + ("refunded", "pending")
    orders = []
    for i in range(1, _DEMO_ORDERS + 1):
        ch = rnd.choices(CHANNELS, weights=weights)[0]
        amount = round(rnd.uniform(20, 900), 2)
        orders.append((i, rnd.randint(1, _DEMO_CUSTOMERS), ch, amount, rnd.choice(statuses),
                       f"2026-07-{rnd.randint(1, 28):02d} {rnd.randint(8, 22):02d}:"
                       f"{rnd.randint(0, 59):02d}:00"))
    return customers, orders


def seed_demo_db(data_dir: str | Path) -> Path:
    """在 data 目录下生成示例 SQLite 库（已存在则不动），返回其路径。"""
    path = Path(data_dir) / DEMO_DB_REL
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    customers, orders = _demo_rows()
    tmp = path.with_suffix(".tmp")
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(
            """
            CREATE TABLE customers (
                id         INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                city       TEXT,
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT
            );
            CREATE TABLE orders (
                id         INTEGER PRIMARY KEY,
                uid        INTEGER NOT NULL REFERENCES customers(id),
                channel    TEXT NOT NULL,
                amount     REAL NOT NULL,
                status     TEXT NOT NULL,
                created_at TEXT
            );
            CREATE INDEX idx_orders_uid ON orders(uid);
            CREATE INDEX idx_orders_channel ON orders(channel);
            """
        )
        conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", customers)
        conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", orders)
        conn.commit()
    finally:
        conn.close()
    tmp.replace(path)
    return path


def example_graph(csv_path: str) -> dict:
    return {
        "nodes": [
            {"id": "n_src_orders", "type": "source", "name": "orders", "x": 30, "y": 40,
             "cfg": {"conn": EXAMPLE_CONN, "sql": "SELECT * FROM orders",
                     "limit": 100000}},
            {"id": "n_src_users", "type": "source", "name": "users", "x": 30, "y": 150,
             "cfg": {"conn": EXAMPLE_CONN,
                     "sql": "SELECT id, name, active FROM customers", "limit": 10000}},
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
    """首次启动播种：示例库与示例 CSV 缺了就生成；workflow 表为空时写入示例流程。

    返回是否播种了示例流程。以「表为空」为条件而非按名判断：用户删除示例后重启
    不会复活（除非删光了所有 workflow）。示例库/CSV 独立于流程始终补齐——示例配置里的
    `demo/shop` 连接靠它才连得上。
    """
    seed_demo_db(data_dir)
    csv_path = Path(data_dir) / EXAMPLE_CSV_REL
    if not csv_path.exists():
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(EXAMPLE_CSV, encoding="utf-8")
    if workflows.list():
        return False
    graph = example_graph(str(csv_path))
    from .workflows import compile_graph
    sources = compile_graph(graph)["sources"]  # 顺带校验模板本身合法
    workflows.save(EXAMPLE_NAME, EXAMPLE_WORKSPACE, "", sources,
                   chart=EXAMPLE_CHART, graph=graph)
    return True
