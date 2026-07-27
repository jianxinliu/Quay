"""workflow 富通知的纯函数测试：截断/markdown 表/payload 拼装。"""

from dbmcp.notify import (
    render_markdown_table,
    render_workflow_notification,
    truncate_notification_body,
)


class TestTruncateBody:
    def test_short_passthrough(self):
        assert truncate_notification_body("hello", 100) == "hello"

    def test_ascii_boundary_truncated(self):
        s = "abcdefghijklmnopqrstuvwxyz" * 200  # 5200 bytes
        out = truncate_notification_body(s, 3000)
        assert len(out.encode("utf-8")) <= 3000
        assert out.endswith("…（已截断）")

    def test_chinese_no_half_char(self):
        s = "你好" * 1000  # 每字符 3 字节 → 6000 bytes
        out = truncate_notification_body(s, 3000)
        # 解码不抛异常（回退到字符边界）
        assert isinstance(out, str)
        assert out.endswith("…（已截断）")
        # 主体全是完整汉字
        prefix = out[: -len("…（已截断）")].rstrip("\n")
        assert all(ch == "你" or ch == "好" for ch in prefix)

    def test_mixed_ascii_and_cjk(self):
        s = ("hello 世界 " * 500)
        out = truncate_notification_body(s, 2000)
        assert len(out.encode("utf-8")) <= 2000
        assert out.endswith("…（已截断）")

    def test_empty_body(self):
        assert truncate_notification_body("", 3000) == ""

    def test_max_bytes_smaller_than_body(self):
        # body 长于 max_bytes → 触发截断
        out = truncate_notification_body("a" * 100, 20)
        assert len(out.encode("utf-8")) <= 20
        assert "…" in out


class TestMarkdownTable:
    def test_basic_table(self):
        out = render_markdown_table(["a", "b"], [[1, 2], [3, 4]])
        assert "| a | b |" in out
        assert "| --- | --- |" in out
        assert "| 1 | 2 |" in out

    def test_escape_pipes_and_newlines(self):
        out = render_markdown_table(["c"], [["a|b\nc"]])
        assert "a\\|b c" in out  # | 转义、\n → 空格

    def test_null_value(self):
        out = render_markdown_table(["a"], [[None], [1]])
        assert "|  |" in out  # None → 空

    def test_limits_rows(self):
        rows = [[i] for i in range(30)]
        out = render_markdown_table(["a"], rows, max_rows=5)
        assert out.count("\n|") <= 7  # header + sep + 5 行
        assert "总共 30 行" in out

    def test_no_columns(self):
        assert "无输出列" in render_markdown_table([], [[1]])


class TestWorkflowNotificationPayload:
    def _run(self, status="ok", rows=None, cols=None, error="", run_id=42):
        return {
            "id": run_id,
            "status": status,
            "error": error,
            "output_preview": {"columns": cols or ["a", "b"], "rows": rows or [[1, 2]]},
        }

    def test_summary_only_ok(self):
        p = render_workflow_notification("wf1", self._run(), ["summary"],
                                         admin_base_url="http://x")
        assert "完成" in p["title"] and "✓" in p["body"]
        assert "输出 1 行 × 2 列" in p["body"]
        assert "http://x/admin/workflows/runs/42" in p["body"]
        assert p["meta"]["deeplink"] == "http://x/admin/workflows/runs/42"
        assert p["meta"]["workflow"] == "wf1"

    def test_summary_only_failed(self):
        p = render_workflow_notification("wf1",
                                         self._run(status="failed", error="boom"),
                                         ["summary"],
                                         admin_base_url="http://x")
        assert "失败" in p["title"] and "✗" in p["body"]
        assert "boom" in p["body"]

    def test_markdown_table_appended(self):
        p = render_workflow_notification("wf1",
                                         self._run(cols=["a", "b"], rows=[[1, 2], [3, 4]]),
                                         ["summary", "markdown_table"],
                                         admin_base_url="http://x")
        assert "| a | b |" in p["body"]

    def test_markdown_table_skipped_when_failed(self):
        p = render_workflow_notification("wf1",
                                         self._run(status="failed", rows=[[1]]),
                                         ["summary", "markdown_table"],
                                         admin_base_url="http://x")
        # failed 时不拼表（output 可能不可靠）
        assert "| a |" not in p["body"]

    def test_xlsx_link_appended(self):
        p = render_workflow_notification(
            "wf1", self._run(), ["summary", "xlsx_link"],
            admin_base_url="http://x",
            download_path="/admin/workflows/runs/42/download/output.xlsx")
        assert "http://x/admin/workflows/runs/42/download/output.xlsx" in p["body"]

    def test_xlsx_link_needs_admin_base_url(self):
        # 没配 admin_base_url → 不拼下载链接（避免相对路径通知里看不明白）
        p = render_workflow_notification("wf1", self._run(), ["summary", "xlsx_link"],
                                         admin_base_url="",
                                         download_path="/admin/workflows/runs/42/download/output.xlsx")
        assert "output.xlsx" not in p["body"]

    def test_body_never_exceeds_3000_bytes(self):
        big_rows = [[f"cell {i}"] * 5 for i in range(1000)]
        p = render_workflow_notification(
            "wf1", self._run(cols=["c1", "c2", "c3", "c4", "c5"], rows=big_rows),
            ["summary", "markdown_table"], admin_base_url="http://x")
        assert len(p["body"].encode("utf-8")) <= 3000

    def test_meta_carries_status_and_workflow(self):
        p = render_workflow_notification("wf1", self._run(status="failed"), ["summary"])
        assert p["meta"]["status"] == "failed"
        assert p["meta"]["workflow"] == "wf1"
        assert p["meta"]["kind"] == "workflow_run"
