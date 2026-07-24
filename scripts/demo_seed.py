#!/usr/bin/env python3
"""生成一个内容丰富的 SQLite 示例库，供 Quay 首次启动开箱即用。

Northwind 风格的迷你电商：8 张有外键关联的表，数据量适中（几十~上百行），
足以演示多表 JOIN、分组聚合、子查询、窗口函数、图表与分析工作台的 DAG。
仅用标准库（sqlite3 + random），无第三方依赖；random 用固定种子，产物可复现。

用法：python demo_seed.py <输出路径>
"""
from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE categories (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT
);
CREATE TABLE suppliers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT, contact TEXT
);
CREATE TABLE employees (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, title TEXT, region TEXT, hired_at TEXT
);
CREATE TABLE customers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT, level TEXT, joined_at TEXT
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    category_id INTEGER, supplier_id INTEGER,
    price REAL, stock INTEGER,
    FOREIGN KEY(category_id) REFERENCES categories(id),
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id)
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY, customer_id INTEGER, employee_id INTEGER,
    order_date TEXT, status TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(employee_id) REFERENCES employees(id)
);
CREATE TABLE order_items (
    id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER,
    qty INTEGER, unit_price REAL, discount REAL,
    FOREIGN KEY(order_id) REFERENCES orders(id),
    FOREIGN KEY(product_id) REFERENCES products(id)
);
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY, product_id INTEGER, customer_id INTEGER,
    rating INTEGER, comment TEXT, created_at TEXT,
    FOREIGN KEY(product_id) REFERENCES products(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE INDEX idx_products_category ON products(category_id);
CREATE INDEX idx_products_supplier ON products(supplier_id);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_employee ON orders(employee_id);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_items_product ON order_items(product_id);
CREATE INDEX idx_reviews_product ON reviews(product_id);
"""

CATEGORIES = [
    (1, "电脑外设", "键盘鼠标等输入设备"),
    (2, "显示设备", "显示器与投影"),
    (3, "音频设备", "耳机音箱麦克风"),
    (4, "存储设备", "硬盘固态与U盘"),
    (5, "办公家具", "桌椅与人体工学"),
    (6, "线缆配件", "扩展坞线材支架"),
    (7, "网络设备", "路由交换与网卡"),
]

SUPPLIERS = [
    (1, "极客电子", "深圳", "chen@geek.example"),
    (2, "云海科技", "上海", "li@yunhai.example"),
    (3, "北方数码", "北京", "wang@bfsm.example"),
    (4, "南岭智造", "广州", "zhao@nanling.example"),
    (5, "蓉城精工", "成都", "zhou@rongcheng.example"),
    (6, "西湖优选", "杭州", "sun@xihu.example"),
]

EMPLOYEES = [
    (1, "孙磊", "销售经理", "华南", "2021-03-01"),
    (2, "钱芳", "高级销售", "华东", "2021-07-15"),
    (3, "吴昊", "销售代表", "华北", "2022-01-10"),
    (4, "郑爽", "销售代表", "西南", "2022-06-20"),
    (5, "冯婷", "高级销售", "华中", "2023-02-05"),
    (6, "褚健", "销售代表", "西北", "2023-09-12"),
]

CATEGORY_PRODUCTS = {
    1: ["机械键盘", "无线鼠标", "静音鼠标", "客制化键盘", "数字小键盘", "触控板"],
    2: ["27寸4K显示器", "曲面带鱼屏", "便携显示器", "24寸办公屏", "电竞高刷屏"],
    3: ["主动降噪耳机", "开放式耳机", "桌面音箱", "USB麦克风", "会议全向麦"],
    4: ["4TB机械硬盘", "1TB固态硬盘", "2TB移动硬盘", "512G U盘", "NVMe扩展卡"],
    5: ["人体工学椅", "电动升降桌", "笔记本支架", "显示器支臂", "脚踏板"],
    6: ["USB-C扩展坞", "HDMI线2米", "雷电4线缆", "理线器套装", "手机支架"],
    7: ["WiFi6路由器", "千兆交换机", "USB网卡", "电力猫", "网线10米"],
}

SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张"
GIVEN = ["伟", "娜", "强", "洋", "静", "帆", "敏", "杰", "磊", "芳",
         "军", "丽", "涛", "艳", "斌", "霞", "波", "娟", "辉", "萍",
         "鹏", "颖", "健", "琳", "宇", "婷", "浩", "雪", "刚", "梅"]
CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安",
          "南京", "苏州", "重庆", "长沙", "青岛", "厦门", "天津"]
LEVELS = ["普通", "普通", "普通", "银牌", "银牌", "金牌"]
STATUSES = ["已完成", "已完成", "已完成", "已发货", "处理中", "已取消"]
COMMENTS = ["很满意，做工扎实", "性价比不错", "物流很快", "手感一般",
            "包装完好，推荐", "比预期好", "偶有小瑕疵", "回购了第二个",
            "客服响应及时", "颜值在线"]


def build(path: str) -> None:
    rng = random.Random(42)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    conn = sqlite3.connect(str(p))
    try:
        cur = conn.cursor()
        cur.executescript(SCHEMA)
        cur.executemany("INSERT INTO categories VALUES (?,?,?)", CATEGORIES)
        cur.executemany("INSERT INTO suppliers VALUES (?,?,?,?)", SUPPLIERS)
        cur.executemany("INSERT INTO employees VALUES (?,?,?,?,?)", EMPLOYEES)

        # 客户 25
        customers = []
        for i in range(1, 26):
            name = rng.choice(SURNAMES) + rng.choice(GIVEN)
            month = rng.randint(1, 12)
            customers.append((i, name, rng.choice(CITIES), rng.choice(LEVELS),
                              f"2023-{month:02d}-{rng.randint(1, 28):02d}"))
        cur.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", customers)

        # 产品：按分类批量生成，供应商随机
        products = []
        pid = 1
        for cat_id, names in CATEGORY_PRODUCTS.items():
            for nm in names:
                price = round(rng.uniform(59, 1999) / 10) * 10 - 1  # 形如 x99/x89
                products.append((pid, nm, cat_id, rng.randint(1, len(SUPPLIERS)),
                                 float(price), rng.randint(0, 500)))
                pid += 1
        cur.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", products)
        price_of = {r[0]: r[4] for r in products}
        n_products = len(products)

        # 订单 60 + 订单明细（每单 1~4 行）
        orders, items = [], []
        item_id = 1
        for oid in range(1, 61):
            cust = rng.randint(1, 25)
            emp = rng.randint(1, len(EMPLOYEES))
            month = rng.randint(1, 6)
            date = f"2024-{month:02d}-{rng.randint(1, 28):02d}"
            orders.append((oid, cust, emp, date, rng.choice(STATUSES)))
            for pid in rng.sample(range(1, n_products + 1), rng.randint(1, 4)):
                disc = rng.choice([0.0, 0.0, 0.0, 0.05, 0.1])
                items.append((item_id, oid, pid, rng.randint(1, 5),
                              price_of[pid], disc))
                item_id += 1
        cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
        cur.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", items)

        # 评价 45
        reviews = []
        for rid in range(1, 46):
            month = rng.randint(2, 6)
            reviews.append((rid, rng.randint(1, n_products), rng.randint(1, 25),
                            rng.randint(2, 5), rng.choice(COMMENTS),
                            f"2024-{month:02d}-{rng.randint(1, 28):02d}"))
        cur.executemany("INSERT INTO reviews VALUES (?,?,?,?,?,?)", reviews)

        conn.commit()
        counts = {t: cur.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("categories", "suppliers", "employees", "customers",
                            "products", "orders", "order_items", "reviews")}
    finally:
        conn.close()
    print(f"[demo_seed] created {p}: " +
          ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "demo.sqlite3")
