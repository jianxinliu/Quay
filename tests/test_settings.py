"""系统设置 store 测试：默认值、校验/夹取、未知键忽略、类型还原。"""

from dbmcp.settings import DEFAULTS, SettingsStore


def test_defaults_returned_when_empty():
    s = SettingsStore(":memory:")
    assert s.get_all() == DEFAULTS


def test_save_and_reload_types():
    s = SettingsStore(":memory:")
    out = s.save({"theme": "light", "redis_page_size": "50"})
    assert out["theme"] == "light"
    assert out["redis_page_size"] == 50  # 还原为 int
    assert out["redis_key_limit"] == DEFAULTS["redis_key_limit"]  # 未改保持默认


def test_unknown_key_ignored():
    s = SettingsStore(":memory:")
    out = s.save({"bogus": "x", "theme": "light"})
    assert "bogus" not in out
    assert out["theme"] == "light"


def test_int_clamped_to_bounds():
    s = SettingsStore(":memory:")
    assert s.save({"redis_page_size": "1"})["redis_page_size"] == 10        # 下界
    assert s.save({"redis_page_size": "999999"})["redis_page_size"] == 2000  # 上界
    assert s.save({"redis_key_limit": "5"})["redis_key_limit"] == 100        # 下界


def test_new_settings_roundtrip_and_bounds():
    s = SettingsStore(":memory:")
    # 新增整型项：类型还原 + 区间夹取
    assert s.save({"sql_font_size": "18"})["sql_font_size"] == 18
    assert s.save({"sql_font_size": "999"})["sql_font_size"] == 24       # 上界
    assert s.save({"sql_max_rows": "1"})["sql_max_rows"] == 10           # 下界
    assert s.save({"redis_scan_count": "999999"})["redis_scan_count"] == 10_000
    assert s.save({"redis_min_dbs": "0"})["redis_min_dbs"] == 1          # 下界
    assert s.save({"ui_font_size": "100"})["ui_font_size"] == 20         # 上界


def test_new_bool_settings_roundtrip():
    s = SettingsStore(":memory:")
    out = s.save({"sql_word_wrap": "on", "redis_msgpack_decode": "false",
                  "audit_auto_refresh": "1", "audit_hide_admin_ui": "no"})
    assert out["sql_word_wrap"] is True
    assert out["redis_msgpack_decode"] is False
    assert out["audit_auto_refresh"] is True
    assert out["audit_hide_admin_ui"] is False


def test_theme_invalid_falls_back_to_dark():
    s = SettingsStore(":memory:")
    assert s.save({"theme": "rainbow"})["theme"] == "dark"


def test_bad_int_falls_back_to_default():
    s = SettingsStore(":memory:")
    assert s.save({"redis_page_size": "abc"})["redis_page_size"] == DEFAULTS["redis_page_size"]


def test_sql_minimap_bool_roundtrip():
    s = SettingsStore(":memory:")
    assert s.get_all()["sql_minimap"] is True  # 默认开启
    # 表单/前端可能传 "false"/"true"/"on"/"1" 等 → 归一化为 bool
    assert s.save({"sql_minimap": "false"})["sql_minimap"] is False
    assert s.save({"sql_minimap": "true"})["sql_minimap"] is True
    assert s.save({"sql_minimap": "on"})["sql_minimap"] is True
    assert s.save({"sql_minimap": "0"})["sql_minimap"] is False
