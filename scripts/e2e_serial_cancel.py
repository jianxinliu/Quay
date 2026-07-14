"""真实 MySQL e2e：验证「按角色分超时（根治 2013）」+「DB 层取消（KILL QUERY）」。

方言相关代码 SQLite 单测跑不到，必须对真实 MySQL 跑 e2e（CLAUDE.md 教训）。

用 `DO SLEEP(n)` 模拟长时间运行的写操作：DO 不是 SELECT，故 MySQL 的
max_execution_time 不约束它——这正是「大 DELETE 无服务端语句超时、只被客户端 socket
read_timeout 打断成 2013」的忠实复现。

前置：本机 docker 里的 dbm-mysql-test（127.0.0.1:13306, root/123456）。
可用环境变量覆盖：DBM_E2E_MYSQL_{HOST,PORT,USER,PW,DB}。

跑法：uv run python scripts/e2e_serial_cancel.py
"""

from __future__ import annotations

import os
import threading
import time

from sqlalchemy.exc import OperationalError

from dbmcp.config import ConnectionConfig, Policy, WriterAccount
from dbmcp.engines import _create_readonly_engine, make_canceller, run_query, run_write

HOST = os.environ.get("DBM_E2E_MYSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("DBM_E2E_MYSQL_PORT", "13306"))
USER = os.environ.get("DBM_E2E_MYSQL_USER", "root")
PW = os.environ.get("DBM_E2E_MYSQL_PW", "123456")
DB = os.environ.get("DBM_E2E_MYSQL_DB", "testdb")

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def _cfg(statement_timeout_s: int, write_timeout_s: int) -> ConnectionConfig:
    return ConnectionConfig(
        engine="mysql", environment="dev", host=HOST, port=PORT, database=DB,
        user=USER, password=f"plain://{PW}",
        writer=WriterAccount(user=USER, password=f"plain://{PW}"),
        policy=Policy(statement_timeout_s=statement_timeout_s, write_timeout_s=write_timeout_s),
    )


def _reader(cfg):
    return _create_readonly_engine(cfg, "reader", cfg.host, cfg.port)


def _writer(cfg):
    return _create_readonly_engine(cfg, "writer", cfg.host, cfg.port)


results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{PASS if ok else FAIL}] {name}{(' — ' + detail) if detail else ''}")
    results.append(ok)


def test_reader_socket_timeout_repro():
    """旧 bug 复现：reader socket read_timeout=2s，长操作（DO SLEEP 4s）在 ~2s 被打断。"""
    cfg = _cfg(statement_timeout_s=2, write_timeout_s=600)
    eng = _reader(cfg)
    t0 = time.time()
    try:
        run_query(eng, "DO SLEEP(4)", max_rows=1)
        check("reader 长操作应超时断开", False, "居然没超时")
    except OperationalError as e:
        dt = time.time() - t0
        # 2013 Lost connection（read operation timed out）在 ~2s 触发
        check("reader 2s socket 超时触发（复现 2013）", dt < 3.5, f"{dt:.1f}s: {str(e)[:60]}")
    eng.dispose()


def test_writer_long_op_completes():
    """修复验证：writer socket 超时=write_timeout_s(600s)，同样的 4s 长操作能跑完不报 2013。"""
    cfg = _cfg(statement_timeout_s=2, write_timeout_s=600)
    eng = _writer(cfg)
    t0 = time.time()
    try:
        run_write(eng, "DO SLEEP(4)")
        dt = time.time() - t0
        # 完成且确实等满了 ~4s（说明没被 reader 的 2s socket / max_execution_time 打断）
        check("writer 长写操作跑完（根治 2013）", 3.5 < dt < 8, f"{dt:.1f}s")
    except OperationalError as e:
        check("writer 长写操作跑完（根治 2013）", False, f"意外超时: {str(e)[:60]}")
    eng.dispose()


def test_cancel_kills_running_query():
    """DB 层取消：writer 跑 DO SLEEP(30)，1.5s 后发 KILL QUERY，应在几秒内中断而非等 30s。"""
    cfg = _cfg(statement_timeout_s=30, write_timeout_s=600)
    eng = _writer(cfg)
    canceller_box: dict = {}
    err_box: dict = {}
    done = threading.Event()

    def on_start(canceller):
        canceller_box["fn"] = canceller

    def work():
        try:
            run_write(eng, "DO SLEEP(30)", on_start=on_start)
        except Exception as e:  # noqa: BLE001
            err_box["e"] = e
        finally:
            done.set()

    th = threading.Thread(target=work, daemon=True)
    t0 = time.time()
    th.start()
    # 等取消器就绪
    for _ in range(200):
        if "fn" in canceller_box:
            break
        time.sleep(0.01)
    time.sleep(1.5)
    got_canceller = "fn" in canceller_box
    if got_canceller:
        canceller_box["fn"]()  # KILL QUERY <cid>
    finished = done.wait(10)
    dt = time.time() - t0
    check("取消器已注册", got_canceller)
    check("KILL 使运行中查询快速中断（未等满 30s）", finished and dt < 9,
          f"{dt:.1f}s, err={type(err_box.get('e')).__name__ if err_box.get('e') else '无'}")
    eng.dispose()


if __name__ == "__main__":
    print(f"连接 MySQL {HOST}:{PORT}/{DB} as {USER}")
    test_reader_socket_timeout_repro()
    test_writer_long_op_completes()
    test_cancel_kills_running_query()
    ok = all(results)
    print(f"\n{'全部通过 ✓' if ok else '有失败 ✗'}  ({sum(results)}/{len(results)})")
    raise SystemExit(0 if ok else 1)
