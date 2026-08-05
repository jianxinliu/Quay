"""真实 e2e：审批等待闭环（真实 MySQL + 真实 HTTP MCP 传输 + 真实管理后台）。

要证明的三件事——单测（进程内 Client + SQLite）证不了的部分：
1. `execute` 在服务端等待期间，**同一进程的管理后台仍能正常响应**——人正是要在那儿点批准，
   等待若占了线程池或阻塞事件循环，后台会一起卡死；
2. 后台点「仅批准」后，等待中的 MCP 调用自动重提并真的改了 MySQL 的数据；
3. 后台点「批准并立即执行」后，等待中的调用收到执行结果，且审批单只被核销一次。

前置：`scripts/e2e_approval_wait.sh` 会起一个隔离实例（8201 + 临时数据目录 + --no-auth），
连的是 config/connections.yaml 里的 local/demo-mysql（dev 环境的本机 MySQL）。
可用环境变量覆盖：DBM_E2E_BASE / DBM_E2E_PROJECT / DBM_E2E_CONN / DBM_MYSQL_PW。

跑法：bash scripts/e2e_approval_wait.sh
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time

import httpx
import pymysql
from fastmcp import Client

BASE = os.environ.get("DBM_E2E_BASE", "http://127.0.0.1:8201")
PROJECT = os.environ.get("DBM_E2E_PROJECT", "local")
CONN = os.environ.get("DBM_E2E_CONN", "demo-mysql")
TABLE = "wait_e2e"

admin = httpx.Client(base_url=BASE, trust_env=False, timeout=30)  # trust_env=False 绕过本机代理


def mysql_conn():
    return pymysql.connect(host="127.0.0.1", port=3306, user="root",
                           password=os.environ["DBM_MYSQL_PW"], database="dbm_e2e")


def sql_exec(*statements: str) -> None:
    with mysql_conn() as c, c.cursor() as cur:
        for s in statements:
            cur.execute(s)
        c.commit()


def flag_of(row_id: int) -> int:
    with mysql_conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT flag FROM {TABLE} WHERE id = %s", (row_id,))
        return cur.fetchone()[0]


def pending_ids() -> list[int]:
    return sorted({int(m) for m in re.findall(r"/admin/approvals/(\d+)",
                                              admin.get("/admin/approvals").text)})


async def decide_when_pending(known: set[int], form: dict) -> int:
    """等审批单出现 → 顺带确认后台此刻是活的 → 提交决策表单（模拟人点按钮）。"""
    for _ in range(300):
        new = [i for i in pending_ids() if i not in known]
        if new:
            cid = new[0]
            detail = admin.get(f"/admin/approvals/{cid}")
            assert detail.status_code == 200, "等待期间后台详情页不可用！"
            assert "批准并立即执行" in detail.text
            r = admin.post(f"/admin/approvals/{cid}/approve", data=form, follow_redirects=False)
            assert r.status_code == 303, f"审批提交失败 {r.status_code}"
            return cid
        await asyncio.sleep(0.1)
    raise AssertionError("审批单一直没出现")


async def main() -> None:
    sql_exec(
        f"DROP TABLE IF EXISTS {TABLE}",
        f"CREATE TABLE {TABLE} (id INT PRIMARY KEY, flag INT)",
        f"INSERT INTO {TABLE} VALUES (1, 1), (2, 1), (3, 1)",
    )
    ok = True
    async with Client(f"{BASE}/mcp") as c:  # 无 elicitation handler → 必走审批单流程
        # ---- 1. 后台「仅批准」→ 等待中的 execute 自动重提执行 ----
        known = set(pending_ids())
        t0 = time.monotonic()
        approve = asyncio.create_task(decide_when_pending(known, {"by": "e2e", "note": "ok"}))
        r = await c.call_tool("execute", {
            "project": PROJECT, "connection": CONN,
            "sql": f"UPDATE {TABLE} SET flag = 0 WHERE id = 1",
            "reason": "e2e wait", "wait_seconds": 90,
        })
        cid = await approve
        took = time.monotonic() - t0
        print(f"[1] status={r.data['status']} rows={r.data.get('affected_rows')} 耗时={took:.1f}s")
        ok &= r.data["status"] == "executed" and r.data["affected_rows"] == 1
        ok &= flag_of(1) == 0 and took < 30  # 批准后应立刻返回，不是等满 90s
        print(f"[1] MySQL flag(1)={flag_of(1)} 期望 0")

        # ---- 2. 后台「批准并立即执行」→ agent 收到结果，审批单只核销一次 ----
        known = set(pending_ids())
        approve = asyncio.create_task(
            decide_when_pending(known, {"by": "e2e", "note": "直接执行", "exec": "1"}))
        r2 = await c.call_tool("execute", {
            "project": PROJECT, "connection": CONN,
            "sql": f"UPDATE {TABLE} SET flag = 0 WHERE id = 2",
            "reason": "e2e exec", "wait_seconds": 90,
        })
        cid2 = await approve
        print(f"[2] status={r2.data['status']} rows={r2.data.get('affected_rows')} "
              f"msg={r2.data.get('message')}")
        ok &= r2.data["status"] == "executed" and r2.data["affected_rows"] == 1
        ok &= "管理后台" in (r2.data.get("message") or "") and flag_of(2) == 0
        st = await c.call_tool("get_change_status", {"change_id": cid2})
        print(f"[2] 审批单状态={st.data['status']} exec={st.data.get('exec_result')}")
        ok &= st.data["status"] == "consumed"

        # ---- 3. 等待超时 → wait_for_change 续等 → 批准后重提执行 ----
        r3 = await c.call_tool("execute", {
            "project": PROJECT, "connection": CONN,
            "sql": f"UPDATE {TABLE} SET flag = 0 WHERE id = 3",
            "reason": "e2e timeout", "wait_seconds": 2,
        })
        cid3 = r3.data["change_id"]
        print(f"[3] 超时返回 status={r3.data['status']} waited={r3.data.get('waited_seconds')} "
              f"url={r3.data.get('approval_url')}")
        ok &= r3.data["status"] == "approval_required"
        ok &= r3.data.get("approval_url", "").endswith(f"/admin/approvals/{cid3}")

        known3 = {i for i in pending_ids() if i != cid3}
        approve = asyncio.create_task(decide_when_pending(known3, {"by": "e2e", "note": "later"}))
        w = await c.call_tool("wait_for_change", {"change_id": cid3, "timeout_seconds": 60})
        await approve
        print(f"[3] wait_for_change → {w.data['status']} by {w.data.get('decided_by')}")
        ok &= w.data["status"] == "approved"
        r4 = await c.call_tool("execute", {
            "project": PROJECT, "connection": CONN,
            "sql": f"UPDATE {TABLE} SET flag = 0 WHERE id = 3", "change_id": cid3,
        })
        print(f"[3] 重提 status={r4.data['status']} rows={r4.data.get('affected_rows')}")
        ok &= r4.data["status"] == "executed" and flag_of(3) == 0
        print(f"[i] 涉及审批单 #{cid} #{cid2} #{cid3}")

    sql_exec(f"DROP TABLE {TABLE}")
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(main())
