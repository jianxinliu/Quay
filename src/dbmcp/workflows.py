"""分析 workflow 存储：工作区取数配方 + 多语句 SQL 脚本，可一键重跑。

定义（JSON 存 SQLite）：
- sources: 保存时从工作区 provenance 自动收集（每个数据集怎么拉的）
- script:  编辑器里的多语句 SQL（分号分隔），运行时逐条执行
- graph:   可视化 DAG（节点+连线，见 compile_graph）；有 graph 时运行以图编译为准
运行 = 按 sources 重拉数据 → 逐条执行 script → 最后一个有结果集的语句作为输出预览。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_workflow (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    workspace   TEXT NOT NULL,
    script      TEXT NOT NULL,
    sources     TEXT NOT NULL DEFAULT '[]',   -- JSON：取数配方列表
    chart       TEXT NOT NULL DEFAULT '',     -- JSON：图表配置（type/x/y/agg），空 = 无
    graph       TEXT NOT NULL DEFAULT '',     -- JSON：DAG 画布（nodes/edges），空 = 纯脚本
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


class WorkflowError(Exception):
    """workflow 操作失败。message 面向使用者。"""


@dataclass
class Workflow:
    id: int
    name: str
    workspace: str
    script: str
    sources: list[dict]
    chart: dict | None
    graph: dict | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "workspace": self.workspace,
                "script": self.script, "sources": self.sources, "chart": self.chart,
                "graph": self.graph,
                "created_at": self.created_at, "updated_at": self.updated_at}


class WorkflowStore:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(analysis_workflow)")}
            for missing in ("chart", "graph"):  # 老库升级
                if missing not in cols:
                    self._conn.execute(
                        f"ALTER TABLE analysis_workflow ADD COLUMN {missing} TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    def save(self, name: str, workspace: str, script: str, sources: list[dict],
             chart: dict | None = None, graph: dict | None = None) -> Workflow:
        name = (name or "").strip()
        if not name:
            raise WorkflowError("workflow 名称不能为空")
        if not (script or "").strip() and not graph:
            raise WorkflowError("workflow 脚本不能为空")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO analysis_workflow"
                " (name, workspace, script, sources, chart, graph, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET workspace = excluded.workspace,"
                " script = excluded.script, sources = excluded.sources,"
                " chart = excluded.chart, graph = excluded.graph, updated_at = excluded.updated_at",
                (name, workspace, script, json.dumps(sources, ensure_ascii=False),
                 json.dumps(chart, ensure_ascii=False) if chart else "",
                 json.dumps(graph, ensure_ascii=False) if graph else "", now, now))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM analysis_workflow WHERE name = ?", (name,)).fetchone()
        return _row(row)

    def get(self, name: str) -> Workflow:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM analysis_workflow WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise WorkflowError(f"workflow {name!r} 不存在")
        return _row(row)

    def list(self) -> list[Workflow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM analysis_workflow ORDER BY updated_at DESC").fetchall()
        return [_row(r) for r in rows]

    def delete(self, name: str) -> None:
        with self._lock:
            cur = self._conn.execute("DELETE FROM analysis_workflow WHERE name = ?", (name,))
            if cur.rowcount == 0:
                raise WorkflowError(f"workflow {name!r} 不存在")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row(row: sqlite3.Row) -> Workflow:
    return Workflow(id=row["id"], name=row["name"], workspace=row["workspace"],
                    script=row["script"], sources=json.loads(row["sources"] or "[]"),
                    chart=json.loads(row["chart"]) if row["chart"] else None,
                    graph=json.loads(row["graph"]) if row["graph"] else None,
                    created_at=row["created_at"], updated_at=row["updated_at"])


# ---------- DAG 画布编译 ----------
#
# graph = {"nodes": [...], "edges": [{"from": id, "to": id, "port": "in|left|right"}]}
# 节点 = {"id", "type", "name", "x", "y", "cfg": {...}}，type ∈：
#   source    cfg: {conn: "project/connection", sql, limit?, schema?}   → 导入为数据集
#   file      cfg: {path}                                               → 文件导入为数据集
#   filter    cfg: {where}          输入 1（in）
#   join      cfg: {kind, on}       输入 2（left/right，SQL 里别名 l / r）
#   aggregate cfg: {group, aggs}    输入 1
#   sql       cfg: {sql}            输入任意（直接按上游节点名引用）
#   output    cfg: {order_by?, limit?}  输入 1，终点（最多一个）
# 非 source/file 节点编译为 CREATE OR REPLACE VIEW "节点名" AS ...，按拓扑序执行；
# 中间结果都是工作区里的视图，可单独预览。

_NODE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _q(name: str) -> str:
    return '"' + name + '"'


def compile_graph(graph: dict) -> dict:
    """把 DAG 编译为可执行计划：{sources, steps: [{node, name, sql}], output_sql}。

    校验：节点名合法且唯一、输入口连接完整、无环。失败抛 WorkflowError（面向使用者）。
    """
    nodes = {n.get("id"): n for n in (graph.get("nodes") or [])}
    if not nodes:
        raise WorkflowError("流程为空：请先添加节点")
    seen_names = set()
    for n in nodes.values():
        name = (n.get("name") or "").strip()
        if not _NODE_NAME_RE.match(name):
            raise WorkflowError(f"节点名 {name!r} 不合法（字母开头，仅字母/数字/下划线）")
        if name.lower() in seen_names:
            raise WorkflowError(f"节点名 {name!r} 重复")
        seen_names.add(name.lower())

    # 入边：to_id -> {port: from_id}
    inputs: dict[str, dict[str, str]] = {nid: {} for nid in nodes}
    for e in graph.get("edges") or []:
        f, t = e.get("from"), e.get("to")
        if f not in nodes or t not in nodes:
            continue  # 悬空边（节点已删）直接忽略
        inputs[t][e.get("port") or "in"] = f

    def _one_input(n: dict) -> dict:
        up = inputs[n["id"]].get("in")
        if not up:
            raise WorkflowError(f"节点「{n['name']}」缺少输入连线")
        return nodes[up]

    # Kahn 拓扑排序
    indeg = {nid: len(inputs[nid]) for nid in nodes}
    order, queue = [], sorted([nid for nid, d in indeg.items() if d == 0])
    downstream: dict[str, list[str]] = {nid: [] for nid in nodes}
    for t, ports in inputs.items():
        for f in ports.values():
            downstream[f].append(t)
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for t in sorted(downstream[nid]):
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    if len(order) < len(nodes):
        raise WorkflowError("流程中存在环，请检查连线")

    sources: list[dict] = []
    steps: list[dict] = []
    outputs = [n for n in nodes.values() if n.get("type") == "output"]
    if len(outputs) > 1:
        raise WorkflowError("最多只能有一个输出节点")
    output_sql = None
    last_view = None

    for nid in order:
        n = nodes[nid]
        typ, name, cfg = n.get("type"), n["name"].strip(), n.get("cfg") or {}
        if typ == "source":
            conn = (cfg.get("conn") or "").strip()
            if "/" not in conn:
                raise WorkflowError(f"取数节点「{name}」未选择连接")
            if not (cfg.get("sql") or "").strip():
                raise WorkflowError(f"取数节点「{name}」缺少 SQL")
            project, connection = conn.split("/", 1)
            sources.append({"kind": "connection", "node": nid, "dataset": name,
                            "project": project, "connection": connection,
                            "sql": cfg["sql"].strip(),
                            "limit": cfg.get("limit"), "schema": cfg.get("schema") or None})
        elif typ == "file":
            if not (cfg.get("path") or "").strip():
                raise WorkflowError(f"文件节点「{name}」缺少路径")
            sources.append({"kind": "file", "node": nid, "dataset": name,
                            "path": cfg["path"].strip()})
        elif typ == "filter":
            up = _one_input(n)
            where = (cfg.get("where") or "").strip()
            if not where:
                raise WorkflowError(f"过滤节点「{name}」缺少 WHERE 条件")
            sql = f"SELECT * FROM {_q(up['name'])} WHERE {where}"
            steps.append({"node": nid, "name": name,
                          "sql": f"CREATE OR REPLACE VIEW {_q(name)} AS {sql}"})
            last_view = name
        elif typ == "join":
            left, right = inputs[nid].get("left"), inputs[nid].get("right")
            if not left or not right:
                raise WorkflowError(f"JOIN 节点「{name}」需要接满左右两个输入")
            on = (cfg.get("on") or "").strip()
            if not on:
                raise WorkflowError(f"JOIN 节点「{name}」缺少 ON 条件（用 l / r 引用左右表）")
            kind = (cfg.get("kind") or "INNER").upper()
            if kind not in ("INNER", "LEFT", "RIGHT", "FULL"):
                raise WorkflowError(f"JOIN 类型 {kind!r} 不支持")
            cols = (cfg.get("select") or "l.*, r.*").strip()
            sql = (f"SELECT {cols} FROM {_q(nodes[left]['name'])} l"
                   f" {kind} JOIN {_q(nodes[right]['name'])} r ON {on}")
            steps.append({"node": nid, "name": name,
                          "sql": f"CREATE OR REPLACE VIEW {_q(name)} AS {sql}"})
            last_view = name
        elif typ == "aggregate":
            up = _one_input(n)
            aggs = (cfg.get("aggs") or "").strip()
            if not aggs:
                raise WorkflowError(f"聚合节点「{name}」缺少聚合表达式（如 count(*) AS n）")
            group = (cfg.get("group") or "").strip()
            select = f"{group}, {aggs}" if group else aggs
            sql = f"SELECT {select} FROM {_q(up['name'])}"
            if group:
                sql += f" GROUP BY {group}"
            steps.append({"node": nid, "name": name,
                          "sql": f"CREATE OR REPLACE VIEW {_q(name)} AS {sql}"})
            last_view = name
        elif typ == "sql":
            raw = (cfg.get("sql") or "").strip().rstrip(";")
            if not raw:
                raise WorkflowError(f"SQL 节点「{name}」内容为空")
            steps.append({"node": nid, "name": name,
                          "sql": f"CREATE OR REPLACE VIEW {_q(name)} AS ({raw})"})
            last_view = name
        elif typ == "output":
            up = _one_input(n)
            sql = f"SELECT * FROM {_q(up['name'])}"
            if (cfg.get("order_by") or "").strip():
                sql += f" ORDER BY {cfg['order_by'].strip()}"
            limit = cfg.get("limit")
            sql += f" LIMIT {int(limit)}" if limit else " LIMIT 1000"
            output_sql = sql
            steps.append({"node": nid, "name": name, "sql": sql})
        else:
            raise WorkflowError(f"未知节点类型 {typ!r}")

    if output_sql is None:  # 没画输出节点：预览最后一个视图（若全是数据源则预览最后的源）
        tail = last_view or (sources[-1]["dataset"] if sources else None)
        if tail:
            output_sql = f"SELECT * FROM {_q(tail)} LIMIT 1000"
            steps.append({"node": None, "name": "预览", "sql": output_sql})
    return {"sources": sources, "steps": steps, "output_sql": output_sql}


def split_statements(script: str) -> list[str]:
    """按分号切分多条语句（跳过引号与注释），供 workflow 逐条执行。"""
    out, start, i, n = [], 0, 0, len(script)
    while i < n:
        c = script[i]
        two = script[i:i + 2]
        if c in ("'", '"'):
            q = c
            i += 1
            while i < n:
                if script[i] == "\\":
                    i += 2
                    continue
                if script[i] == q:
                    if q == "'" and script[i + 1:i + 2] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif two == "--":
            while i < n and script[i] != "\n":
                i += 1
        elif two == "/*":
            i += 2
            while i < n and script[i:i + 2] != "*/":
                i += 1
            i += 2
        elif c == ";":
            stmt = script[start:i].strip()
            if stmt:
                out.append(stmt)
            i += 1
            start = i
        else:
            i += 1
    tail = script[start:].strip()
    if tail:
        out.append(tail)
    return out
