"""引擎会话设置的回归测试。

针对真实 MySQL e2e 暴露的 bug：max_execution_time 与 READ ONLY 被逗号拼进一条
SET 语句导致 1064 语法错误。SQLite 单测跑不到 MySQL 方言，故用纯函数断言语句拆分。
"""

from dbmcp.engines import mysql_session_statements


def test_reader_statements_are_separate():
    stmts = mysql_session_statements(timeout_s=30, readonly=True)
    assert len(stmts) == 2
    # 不能有任何一条语句同时包含变量赋值和 TRANSACTION（即不能逗号拼接）
    for s in stmts:
        assert not ("max_execution_time" in s and "TRANSACTION" in s), f"语句被错误拼接: {s}"
    assert stmts[0] == "SET SESSION max_execution_time = 30000"
    assert stmts[1] == "SET SESSION TRANSACTION READ ONLY"


def test_writer_has_no_readonly():
    stmts = mysql_session_statements(timeout_s=10, readonly=False)
    assert stmts == ["SET SESSION max_execution_time = 10000"]
    assert all("READ ONLY" not in s for s in stmts)
