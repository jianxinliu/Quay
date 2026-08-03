"""AI 辅助生成 SQL 的纯函数 + provider 调用测试（用假子进程 monkeypatch，不真调 CLI）。"""

import json
import subprocess

import pytest

from dbmcp import ai


# ---------- prompt 拼装（纯函数） ----------

def test_build_sql_prompt_plain_has_dialect_ddl_question_contract():
    p = ai.build_sql_prompt("你是专家", "mysql",
                            [("orders", "CREATE TABLE orders(id INT)")],
                            "统计订单数", explain=False)
    assert "你是专家" in p
    assert "mysql" in p
    assert "CREATE TABLE orders" in p
    assert "统计订单数" in p
    assert ai._CONTRACT_PLAIN in p
    assert ai._CONTRACT_EXPLAIN not in p


def test_build_sql_prompt_explain_switches_contract_and_can_add_samples():
    p = ai.build_sql_prompt("sys", "postgres", [("t", "DDL")], "q",
                            explain=True, samples={"t": "id\n1\n2"})
    assert ai._CONTRACT_EXPLAIN in p
    assert ai._CONTRACT_PLAIN not in p
    assert "样本数据" in p
    assert "id\n1\n2" in p


def test_build_sql_prompt_empty_system_falls_back_to_default():
    p = ai.build_sql_prompt("", "sqlite", [], "q", explain=False)
    assert ai.DEFAULT_SQL_PROMPT.strip()[:10] in p


def test_build_followup_prompt_has_no_ddl_only_adjustment():
    p = ai.build_followup_prompt("改成按周分组", explain=False)
    assert "改成按周分组" in p
    assert "上一条 SQL" in p
    assert "CREATE TABLE" not in p
    assert ai._CONTRACT_PLAIN in p


# ---------- 输出解析 ----------

def test_parse_plain_strips_fences():
    r = ai.parse_ai_output("```sql\nSELECT 1\n```", explain=False)
    assert r.sql == "SELECT 1"
    assert r.explanation == ""


def test_parse_explain_json():
    raw = json.dumps({"sql": "SELECT 1", "explanation": "走主键"})
    r = ai.parse_ai_output(raw, explain=True)
    assert r.sql == "SELECT 1"
    assert r.explanation == "走主键"


def test_parse_explain_json_wrapped_in_fences_and_prose():
    raw = "这是结果：\n```json\n{\"sql\":\"SELECT 2\",\"explanation\":\"e\"}\n```"
    r = ai.parse_ai_output(raw, explain=True)
    assert r.sql == "SELECT 2"
    assert r.explanation == "e"


def test_parse_explain_fallback_when_not_json_treats_all_as_sql():
    r = ai.parse_ai_output("SELECT 3", explain=True)
    assert r.sql == "SELECT 3"
    assert r.explanation == ""


# ---------- provider 调用（假子进程） ----------

def _fake_run(monkeypatch, *, stdout="", stderr="", returncode=0,
              exc=None, write_out=None):
    """monkeypatch subprocess.run；write_out(cmd) 可往 -o 文件写内容（codex 用）。"""
    def fake(cmd, **kw):  # noqa: ANN001
        if exc is not None:
            raise exc
        if write_out is not None:
            write_out(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    monkeypatch.setattr(ai.subprocess, "run", fake)


def test_run_ai_claude_success_returns_text_and_session(monkeypatch):
    payload = {"is_error": False, "result": "SELECT 1", "session_id": "sid-abc"}
    _fake_run(monkeypatch, stdout=json.dumps(payload))
    text, sid = ai.run_ai("p", provider="claude", model="claude-sonnet-5", timeout=10)
    assert text == "SELECT 1"
    assert sid == "sid-abc"


def test_run_ai_claude_is_error_raises(monkeypatch):
    payload = {"is_error": True, "result": "403 not allowed"}
    _fake_run(monkeypatch, stdout=json.dumps(payload))
    with pytest.raises(ai.AIError, match="403"):
        ai.run_ai("p", provider="claude", model="", timeout=10)


def test_run_ai_claude_resume_passes_flag(monkeypatch):
    seen = {}

    def fake(cmd, **kw):  # noqa: ANN001
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps({"is_error": False, "result": "SELECT 9", "session_id": "s2"}), "")
    monkeypatch.setattr(ai.subprocess, "run", fake)
    ai.run_ai("p", provider="claude", model="", timeout=10, session_id="s1")
    assert "--resume" in seen["cmd"]
    assert "s1" in seen["cmd"]


def test_run_ai_codex_reads_output_file_and_session(monkeypatch):
    def write_out(cmd):
        path = cmd[cmd.index("-o") + 1]
        with open(path, "w", encoding="utf-8") as f:
            f.write("SELECT 5")
    sid_uuid = "019f6b6b-8513-7981-b817-ce748e0899f4"
    _fake_run(monkeypatch, stderr=f"session id: {sid_uuid}\n", write_out=write_out)
    text, sid = ai.run_ai("p", provider="codex", model="gpt-x", timeout=10)
    assert text == "SELECT 5"
    assert sid == sid_uuid


def test_run_ai_codex_empty_output_raises(monkeypatch):
    _fake_run(monkeypatch, stderr='ERROR: {"status":400}')
    with pytest.raises(ai.AIError, match="codex"):
        ai.run_ai("p", provider="codex", model="", timeout=10)


def test_run_ai_timeout_raises_aierror(monkeypatch):
    _fake_run(monkeypatch, exc=subprocess.TimeoutExpired("claude", 10))
    with pytest.raises(ai.AIError, match="超时"):
        ai.run_ai("p", provider="claude", model="", timeout=10)


def test_run_ai_missing_cli_raises_aierror(monkeypatch):
    _fake_run(monkeypatch, exc=FileNotFoundError())
    with pytest.raises(ai.AIError, match="找不到"):
        ai.run_ai("p", provider="claude", model="", timeout=10)


def test_run_ai_unknown_provider_raises():
    with pytest.raises(ai.AIError, match="provider"):
        ai.run_ai("p", provider="bogus", model="", timeout=10)


# ---------- provider=api（直连 HTTP，mock httpx；key 优先 keyring）----------

class _FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.text = status, payload, json.dumps(payload)

    def json(self):
        return self._p


def _fake_httpx(monkeypatch, *, status=200, payload=None, record=None):
    import httpx
    ai._API_SESSIONS.clear()

    def post(url, json=None, headers=None, timeout=None):  # noqa: A002
        if record is not None:
            record.append({"url": url, "headers": headers, "body": json})
        return _FakeResp(status, payload or {})
    monkeypatch.setattr(httpx, "post", post)


def _stub_key(monkeypatch, value="sk-test"):
    """让 _resolve_api_key 返回固定 key（绕过真实 keyring/env）。"""
    monkeypatch.setattr(ai, "_resolve_api_key", lambda key_env: value)


def test_run_api_anthropic_format(monkeypatch):
    rec = []
    _fake_httpx(monkeypatch, payload={"content": [{"type": "text", "text": "SELECT 1"}]}, record=rec)
    _stub_key(monkeypatch, "sk-anthropic")
    text, sid = ai.run_ai("hi", provider="api", model="claude-sonnet-5", timeout=10,
                          api_format="anthropic", api_base="https://api.anthropic.com")
    assert text == "SELECT 1" and sid
    assert rec[0]["url"].endswith("/v1/messages")
    assert rec[0]["headers"]["x-api-key"] == "sk-anthropic"
    assert rec[0]["body"]["model"] == "claude-sonnet-5" and "max_tokens" in rec[0]["body"]


def test_run_api_openai_format(monkeypatch):
    rec = []
    _fake_httpx(monkeypatch, payload={"choices": [{"message": {"content": "SELECT 2"}}]}, record=rec)
    _stub_key(monkeypatch, "sk-oai")
    text, _ = ai.run_ai("hi", provider="api", model="gpt-4o", timeout=10,
                        api_format="openai", api_base="https://api.openai.com")
    assert text == "SELECT 2"
    assert rec[0]["url"].endswith("/v1/chat/completions")
    assert rec[0]["headers"]["Authorization"] == "Bearer sk-oai"


def test_run_api_missing_key_raises(monkeypatch):
    _fake_httpx(monkeypatch, payload={})
    _stub_key(monkeypatch, "")  # 无 keyring 也无 env
    with pytest.raises(ai.AIError, match="API key"):
        ai.run_ai("hi", provider="api", model="", timeout=10)


def test_run_api_non_200_raises(monkeypatch):
    _fake_httpx(monkeypatch, status=401, payload={"error": "bad key"})
    _stub_key(monkeypatch)
    with pytest.raises(ai.AIError, match="401"):
        ai.run_ai("hi", provider="api", model="", timeout=10)


def test_run_api_session_continuity(monkeypatch):
    rec = []
    _fake_httpx(monkeypatch, payload={"content": [{"type": "text", "text": "ok"}]}, record=rec)
    _stub_key(monkeypatch)
    _, sid = ai.run_ai("first", provider="api", model="m", timeout=10)
    assert len(rec[0]["body"]["messages"]) == 1        # 首轮只有一条 user
    ai.run_ai("second", provider="api", model="m", timeout=10, session_id=sid)
    msgs = rec[1]["body"]["messages"]
    assert len(msgs) == 3                              # user1 + assistant1 + user2
    assert msgs[0]["content"] == "first" and msgs[-1]["content"] == "second"


def test_resolve_api_key_prefers_keyring(monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_password", lambda svc, acct: "from-keyring")
    monkeypatch.setenv("MY_KEY", "from-env")
    assert ai._resolve_api_key("MY_KEY") == "from-keyring"  # keyring 优先


def test_resolve_api_key_falls_back_to_env(monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_password", lambda svc, acct: None)
    monkeypatch.setenv("MY_KEY", "from-env")
    assert ai._resolve_api_key("MY_KEY") == "from-env"


# ---------- generate_sql 路由：首轮发 DDL，追问不发 ----------

def test_generate_sql_first_turn_sends_ddl(monkeypatch):
    captured = {}

    def fake_run_ai(prompt, **kw):  # noqa: ANN001
        captured["prompt"] = prompt
        captured["session_id"] = kw.get("session_id")
        return "SELECT 1", "new-sid"
    monkeypatch.setattr(ai, "run_ai", fake_run_ai)
    r = ai.generate_sql(system_prompt="s", dialect="mysql",
                        ddls=[("orders", "CREATE TABLE orders(id INT)")],
                        question="q", explain=False, samples=None,
                        provider="claude", model="", timeout=10)
    assert "CREATE TABLE orders" in captured["prompt"]
    assert captured["session_id"] is None
    assert r.sql == "SELECT 1"
    assert r.session_id == "new-sid"


# ---------- workflow（DAG）生成 ----------

def test_build_workflow_prompt_has_format_conns_ddl_question():
    p = ai.build_workflow_prompt("sys", "mysql", ["demo/main", "demo/other"],
                                 [("orders", "CREATE TABLE orders(id INT)")], "按用户聚合")
    assert "nodes" in p and "edges" in p           # 格式文档
    assert "demo/main" in p and "demo/other" in p  # 可用连接
    assert "CREATE TABLE orders" in p              # 表结构
    assert "按用户聚合" in p                         # 需求


def test_build_workflow_repair_prompt_carries_error():
    p = ai.build_workflow_repair_prompt("节点名 'x y' 不合法")
    assert "x y" in p and "完整" in p


def test_build_workflow_prompt_includes_schema_hint_when_provided():
    """用户已选库时，prompt 必须显式告知 AI source cfg 要带 schema。"""
    p = ai.build_workflow_prompt("sys", "mysql", ["demo/main"],
                                 [("orders", "CREATE TABLE orders(id INT)")],
                                 "按天聚合", schema="dbm_e2e", default_conn="demo/main")
    assert "dbm_e2e" in p                     # 库名进 prompt
    assert '"schema"' in p or 'schema=' in p or 'schema"' in p  # 提示 cfg.schema
    assert "demo/main" in p                   # 默认连接进 prompt


def test_build_workflow_prompt_no_schema_hint_when_omitted():
    """未指定库时不应捏造 schema 提示。"""
    p = ai.build_workflow_prompt("sys", "mysql", ["demo/main"], [], "按天聚合")
    assert "用户已选库" not in p


def test_build_workflow_prompt_modify_mode_when_current_graph_provided():
    """current_graph 非空 = 修改模式：prompt 里带上当前图 JSON + 修改规则说明。"""
    cur = {"nodes": [{"id": "a", "type": "source", "name": "orders",
                      "cfg": {"conn": "demo/main", "sql": "SELECT * FROM orders"}}],
           "edges": []}
    p = ai.build_workflow_prompt("sys", "mysql", ["demo/main"], [], "把 orders 按天聚合",
                                 current_graph=cur)
    assert "修改模式" in p                              # 明确修改模式
    assert "orders" in p and "SELECT * FROM orders" in p  # 当前图 JSON 出现
    assert "尽量保留" in p or "保留原节点" in p           # 修改规则
    assert "完整" in p                                 # 要求返回完整图


def test_build_workflow_prompt_new_mode_when_current_graph_empty():
    """空 nodes 视为新建模式，不进修改分支。"""
    p1 = ai.build_workflow_prompt("sys", "mysql", ["demo/main"], [], "q",
                                  current_graph={"nodes": [], "edges": []})
    p2 = ai.build_workflow_prompt("sys", "mysql", ["demo/main"], [], "q",
                                  current_graph=None)
    assert "修改模式" not in p1 and "修改模式" not in p2


def test_generate_workflow_parses_graph(monkeypatch):
    graph = {"nodes": [{"id": "a", "type": "source", "name": "o", "cfg": {}}], "edges": []}
    monkeypatch.setattr(ai, "run_ai", lambda *a, **k: (json.dumps(graph), "sid-w"))
    g, sid = ai.generate_workflow(system_prompt="s", dialect="mysql", connections=["demo/main"],
                                  ddls=[], question="q", provider="claude", model="", timeout=10)
    assert g["nodes"][0]["name"] == "o"
    assert g.get("edges") == []
    assert sid == "sid-w"


def test_generate_workflow_repair_uses_error_and_session(monkeypatch):
    seen = {}

    def fake_run_ai(prompt, **kw):  # noqa: ANN001
        seen["prompt"] = prompt
        seen["session_id"] = kw.get("session_id")
        return json.dumps({"nodes": [], "edges": []}), "sid2"
    monkeypatch.setattr(ai, "run_ai", fake_run_ai)
    ai.generate_workflow(system_prompt="s", dialect="mysql", connections=[], ddls=[],
                         question="q", provider="claude", model="", timeout=10,
                         repair_error="环检测失败", session_id="sid1")
    assert "环检测失败" in seen["prompt"]
    assert seen["session_id"] == "sid1"


def test_generate_workflow_bad_output_raises(monkeypatch):
    monkeypatch.setattr(ai, "run_ai", lambda *a, **k: ("这不是 JSON", ""))
    with pytest.raises(ai.AIError, match="未按格式"):
        ai.generate_workflow(system_prompt="s", dialect="mysql", connections=[], ddls=[],
                             question="q", provider="claude", model="", timeout=10)


def test_generate_sql_followup_omits_ddl(monkeypatch):
    captured = {}

    def fake_run_ai(prompt, **kw):  # noqa: ANN001
        captured["prompt"] = prompt
        return "SELECT 2", "sid-1"
    monkeypatch.setattr(ai, "run_ai", fake_run_ai)
    ai.generate_sql(system_prompt="s", dialect="mysql",
                    ddls=[("orders", "CREATE TABLE orders(id INT)")],
                    question="改成按周", explain=False, samples=None,
                    provider="claude", model="", timeout=10, session_id="sid-1")
    assert "CREATE TABLE orders" not in captured["prompt"]
    assert "改成按周" in captured["prompt"]
