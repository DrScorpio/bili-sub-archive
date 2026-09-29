"""配置测试：TOML 分层、环境变量、命令行覆盖、校验与错误提示。"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from bili_sub_archive.config import load_config
from bili_sub_archive.errors import ConfigError
from tests import drop_workspace, make_workspace, strip_config_env


def clean_env() -> dict:
    return strip_config_env()


class ConfigTestBase(unittest.TestCase):
    def setUp(self):
        self._saved_env = clean_env()
        self.root = make_workspace("config")

    def tearDown(self):
        drop_workspace(self.root)
        os.environ.update(self._saved_env)

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path


class DefaultsTest(ConfigTestBase):
    def test_defaults(self):
        loaded = load_config(root=self.root, overrides={"uid": 42})
        cfg = loaded.config
        self.assertEqual(cfg.uid, 42)
        self.assertEqual(cfg.enabled_kinds, ["dynamic", "video", "article"])
        self.assertAlmostEqual(cfg.interval_seconds, 1.2)
        self.assertIsNone(cfg.latest)
        self.assertEqual(cfg.output_dir, Path("output"))
        self.assertFalse(cfg.asr_enabled)
        self.assertEqual(cfg.config_files, [])

    def test_missing_uid_raises(self):
        with self.assertRaises(ConfigError):
            load_config(root=self.root, overrides={})

    def test_all_kinds_disabled_raises(self):
        with self.assertRaises(ConfigError):
            load_config(root=self.root, overrides={"uid": 1, "kinds": []})

    def test_unknown_kind_raises(self):
        with self.assertRaises(ConfigError):
            load_config(root=self.root, overrides={"uid": 1, "kinds": ["dynamic", "live"]})

    def test_date_order_raises(self):
        from datetime import date

        with self.assertRaises(ConfigError):
            load_config(root=self.root, overrides={
                "uid": 1, "date_from": date(2026, 10, 1), "date_to": date(2026, 9, 1)})

    def test_small_interval_warns(self):
        loaded = load_config(root=self.root, overrides={"uid": 1, "interval_seconds": 0.2})
        self.assertTrue(any("风控" in w for w in loaded.config.warnings))


class TomlTest(ConfigTestBase):
    def test_base_and_local_merge_with_local_winning(self):
        self.write("config.toml", """
[account]
uid = 111

[output]
dir = "out_base"
download_images = true

[request]
interval_seconds = 2.0
retries = 5

[scope]
video = false
""")
        self.write("config.local.toml", """
[output]
dir = "out_local"
download_images = false

[request]
interval_seconds = 1.5
""")
        loaded = load_config(root=self.root)
        cfg = loaded.config
        self.assertEqual(cfg.uid, 111)
        self.assertEqual(cfg.output_dir, self.root / "out_local")
        self.assertFalse(cfg.download_images)
        self.assertAlmostEqual(cfg.interval_seconds, 1.5)   # local 覆盖 base
        self.assertEqual(cfg.retries, 5)                    # base 保留
        self.assertFalse(cfg.kinds["video"])
        self.assertTrue(cfg.kinds["dynamic"])
        self.assertEqual([p.name for p in cfg.config_files],
                         ["config.toml", "config.local.toml"])

    def test_filter_and_discovery_sections(self):
        self.write("config.toml", """
[account]
uid = 5

[filter]
from = "2026-01-01"
to = "2026-09-23"
latest = 12

[discovery]
probe_video_access = false
scan_charging_video_subset = false

[asr]
enabled = true
model = "medium"

[download]
video_workers = 7
""")
        cfg = load_config(root=self.root).config
        self.assertEqual(cfg.date_from.isoformat(), "2026-01-01")
        self.assertEqual(cfg.date_to.isoformat(), "2026-09-23")
        self.assertEqual(cfg.latest, 12)
        self.assertFalse(cfg.probe_video_access)
        self.assertFalse(cfg.scan_charging_video_subset)
        self.assertTrue(cfg.asr_enabled)
        self.assertEqual(cfg.asr_model, "medium")
        self.assertEqual(cfg.video_workers, 7)
        self.assertIn("日期 2026-01-01 ~ 2026-09-23", cfg.describe_filter())
        self.assertIn("最新 12 条", cfg.describe_filter())

    def test_absolute_output_dir_kept(self):
        absolute = str(self.root / "abs_out")
        # TOML 字面量字符串（单引号）避免 Windows 路径反斜杠被当作转义
        self.write("config.toml", f"[account]\nuid = 1\n\n[output]\ndir = '{absolute}'\n")
        cfg = load_config(root=self.root).config
        self.assertEqual(cfg.output_dir, Path(absolute))

    def test_invalid_toml_raises(self):
        self.write("config.toml", "[account\nuid = 1")
        with self.assertRaises(ConfigError):
            load_config(root=self.root)

    def test_explicit_missing_config_raises(self):
        with self.assertRaises(ConfigError):
            load_config(root=self.root, config_file="nope.toml", overrides={"uid": 1})

    def test_explicit_config_file_read_only(self):
        path = self.write("custom.toml", "[account]\nuid = 77\n")
        loaded = load_config(root=self.root, config_file=path)
        self.assertEqual(loaded.config.uid, 77)

    def test_bad_int_raises(self):
        self.write("config.toml", '[account]\nuid = 1\n\n[request]\nretries = "many"\n')
        with self.assertRaises(ConfigError):
            load_config(root=self.root)

    def test_bad_date_raises(self):
        self.write("config.toml", '[account]\nuid = 1\n\n[filter]\nfrom = "昨天"\n')
        with self.assertRaises(ConfigError):
            load_config(root=self.root)


class EnvAndOverrideTest(ConfigTestBase):
    def test_env_cookie_and_uid(self):
        os.environ["BSA_COOKIE"] = "SESSDATA=abcdef123456; bili_jct=zzzz1111"
        os.environ["BSA_UID"] = "999"
        loaded = load_config(root=self.root)
        self.assertEqual(loaded.config.uid, 999)
        self.assertTrue(loaded.credentials.has_cookie)
        self.assertEqual(loaded.credentials.source, "env:BSA_COOKIE")
        self.assertIn("SESSDATA", loaded.credentials.cookie_field_names)

        # 改名兼容（SubVideo → bili-sub-archive）：旧前缀 SUBVIDEO_* 仍被接受
        os.environ.pop("BSA_COOKIE")
        os.environ.pop("BSA_UID")
        os.environ["SUBVIDEO_COOKIE"] = "SESSDATA=legacy123456"
        os.environ["SUBVIDEO_UID"] = "777"
        legacy = load_config(root=self.root)
        self.assertEqual(legacy.config.uid, 777)
        self.assertEqual(legacy.credentials.source, "env:SUBVIDEO_COOKIE")

    def test_toml_cookie_used_when_no_env(self):
        self.write("config.local.toml", '[account]\nuid = 8\ncookie = "SESSDATA=aaaaaa111111"\n')
        loaded = load_config(root=self.root)
        self.assertTrue(loaded.credentials.has_cookie)
        self.assertEqual(loaded.credentials.source, "config")

    def test_cli_overrides_toml(self):
        self.write("config.toml", """
[account]
uid = 1

[filter]
latest = 30

[request]
interval_seconds = 3.0
""")
        cfg = load_config(root=self.root, overrides={
            "uid": 2, "latest": 5, "interval_seconds": 0.9,
            "kinds": ["dynamic"], "date_from": None}).config
        self.assertEqual(cfg.uid, 2)
        self.assertEqual(cfg.latest, 5)
        self.assertAlmostEqual(cfg.interval_seconds, 0.9)
        self.assertEqual(cfg.enabled_kinds, ["dynamic"])

    def test_env_output_dir_and_llm(self):
        os.environ["BSA_OUTPUT_DIR"] = str(self.root / "env_out")
        os.environ["BSA_LLM_BASE_URL"] = "https://api.example.com/v1"
        os.environ["BSA_LLM_MODEL"] = "test-model"
        cfg = load_config(root=self.root, overrides={"uid": 1}).config
        self.assertEqual(cfg.output_dir, self.root / "env_out")
        self.assertEqual(cfg.summary_base_url, "https://api.example.com/v1")
        self.assertEqual(cfg.summary_model, "test-model")

        # 改名兼容：旧前缀同样生效；新旧同时存在时以新前缀为准
        os.environ.pop("BSA_OUTPUT_DIR")
        os.environ["SUBVIDEO_OUTPUT_DIR"] = str(self.root / "legacy_out")
        os.environ["BSA_OUTPUT_DIR"] = str(self.root / "env_out")
        both = load_config(root=self.root, overrides={"uid": 1}).config
        self.assertEqual(both.output_dir, self.root / "env_out")
        os.environ.pop("BSA_OUTPUT_DIR")
        legacy = load_config(root=self.root, overrides={"uid": 1}).config
        self.assertEqual(legacy.output_dir, self.root / "legacy_out")

    def test_bad_env_interval_warns(self):
        os.environ["BSA_REQUEST_INTERVAL"] = "快一点"
        cfg = load_config(root=self.root, overrides={"uid": 1}).config
        self.assertTrue(any("BSA_REQUEST_INTERVAL" in w for w in cfg.warnings))

        # 改名兼容：用旧名时，告警里报的也必须是实际生效的那个变量名
        os.environ.pop("BSA_REQUEST_INTERVAL")
        os.environ["SUBVIDEO_REQUEST_INTERVAL"] = "快一点"
        legacy = load_config(root=self.root, overrides={"uid": 1}).config
        self.assertTrue(any("SUBVIDEO_REQUEST_INTERVAL" in w for w in legacy.warnings))

    def test_require_txt_fallback(self):
        (self.root / "require.txt").write_text("cookie\nSESSDATA=abcdef123456; bili_jct=zz\n\nmid\n321\n",
                                              encoding="utf-8")
        loaded = load_config(root=self.root)
        self.assertEqual(loaded.config.uid, 321)
        self.assertTrue(loaded.credentials.has_cookie)
        self.assertEqual(loaded.credentials.source, "require.txt")


class LlmKeyTest(ConfigTestBase):
    """LLM 密钥：配置文件里有配置项（[summary] api_key），且优先级正确。"""

    def test_api_key_from_local_config(self):
        self.write("config.local.toml", """
[account]
uid = 8

[summary]
base_url = "https://api.example.com/v1"
model = "gpt-4o-mini"
api_key = "unit-test-config-key"
""")
        loaded = load_config(root=self.root)
        self.assertTrue(loaded.credentials.has_llm_key)
        self.assertEqual(loaded.credentials.llm_key_source, "config")
        self.assertEqual(loaded.credentials.llm_api_key, "unit-test-config-key")
        self.assertTrue(loaded.config.summary_configured)

    def test_llm_api_key_alias_works(self):
        self.write("config.local.toml",
                   '[account]\nuid = 8\n\n[summary]\nllm_api_key = "unit-test-alias-key"\n')
        loaded = load_config(root=self.root)
        self.assertTrue(loaded.credentials.has_llm_key)
        self.assertEqual(loaded.credentials.llm_key_source, "config")

    def test_env_key_beats_config(self):
        self.write("config.local.toml",
                   '[account]\nuid = 8\n\n[summary]\napi_key = "unit-test-config-key"\n')
        os.environ["BSA_LLM_API_KEY"] = "unit-test-env-key"
        loaded = load_config(root=self.root)
        self.assertEqual(loaded.credentials.llm_api_key, "unit-test-env-key")
        self.assertEqual(loaded.credentials.llm_key_source, "env:BSA_LLM_API_KEY")

    def test_key_in_wrong_section_warns_with_hint(self):
        """密钥放错段（例如 [llm]）不能静默失效：告警里必须指到 [summary] api_key。"""
        self.write("config.local.toml",
                   '[account]\nuid = 8\n\n[llm]\napi_key = "unit-test-misplaced-key"\n')
        loaded = load_config(root=self.root)
        self.assertFalse(loaded.credentials.has_llm_key)   # 确实没被读取
        text = "\n".join(loaded.config.warnings)
        self.assertIn("[llm]", text)
        self.assertIn("[summary] api_key", text)
        self.assertIn("config.local.toml", text)           # 告警要指出是哪个文件

    def test_unknown_key_in_known_section_warns(self):
        self.write("config.toml", '[account]\nuid = 8\n\n[summary]\napi_ky = "typo"\n')
        cfg = load_config(root=self.root).config
        self.assertTrue(any("api_ky" in w for w in cfg.warnings), cfg.warnings)

    def test_template_and_local_expose_api_key_item(self):
        """两个配置文件都要有可填写的 api_key 配置项（本 bug 的回归测试）。"""

        for name in ("config.example.toml", "config.local.toml"):
            path = Path(__file__).resolve().parents[1] / name
            if not path.exists():          # config.local.toml 不入库，允许缺失
                continue
            text = path.read_text(encoding="utf-8")
            summary = text.split("[summary]", 1)[1]
            self.assertRegex(summary, r"(?m)^\s*api_key\s*=", f"{name} 的 [summary] 段缺少 api_key")


class ConfigWarningSurfacingTest(ConfigTestBase):
    """配置告警必须进运行摘要，否则"配错了"这件事只存在于内存里。"""

    def test_config_warnings_reach_run_summary(self):
        from bili_sub_archive.models import RunSummary
        from bili_sub_archive.runner import Runner

        self.write("config.local.toml", '[account]\nuid = 8\n\n[llm]\napi_key = "unit-test-typo-key"\n')
        loaded = load_config(root=self.root)
        self.assertTrue(loaded.config.warnings)

        runner = Runner(loaded, logger=None, root=self.root)
        summary = RunSummary(run_id="test", mode="sync", uid=8)
        runner._adopt_config_warnings(summary)
        self.assertIn(loaded.config.warnings[0], summary.warnings)

        before = len(summary.warnings)
        runner._adopt_config_warnings(summary)          # 幂等：不重复堆积
        self.assertEqual(len(summary.warnings), before)


class MindmapStyleConfigTest(ConfigTestBase):
    """导图样式配置：取值校验、路径解析、命令行覆盖（样式是"改了就该重渲染"的输入）。"""

    def test_style_defaults_to_empty_meaning_builtin(self):
        cfg = load_config(root=self.root, overrides={"uid": 1}).config
        self.assertEqual(cfg.mindmap_style, "")
        from bili_sub_archive.summarize.mindmap_style import DEFAULT_STYLE
        self.assertEqual(DEFAULT_STYLE, "paper")

    def test_style_and_font_are_loaded_from_toml(self):
        self.write("config.toml", """
[account]
uid = 7

[mindmap]
style = "dark"
font_size = 21
font_family = "Source Han Sans SC"
""")
        cfg = load_config(root=self.root).config
        self.assertEqual(cfg.mindmap_style, "dark")
        self.assertEqual(cfg.mindmap_font_size, 21)
        self.assertEqual(cfg.mindmap_font_family, "Source Han Sans SC")

    def test_unknown_style_is_config_error(self):
        self.write("config.toml", '[account]\nuid = 7\n\n[mindmap]\nstyle = "cute"\n')
        with self.assertRaises(ConfigError) as ctx:
            load_config(root=self.root)
        self.assertIn("style", str(ctx.exception))
        self.assertIn("paper", str(ctx.exception))

    def test_custom_style_files_resolve_relative_to_config(self):
        self.write("mine.json", "{}")
        self.write("mine.css", "/* x */")
        self.write("config.toml", """
[account]
uid = 7

[mindmap]
style = "paper"
config_file = "mine.json"
css_file = "mine.css"
""")
        cfg = load_config(root=self.root).config
        self.assertEqual(cfg.mindmap_config_file, str(self.root / "mine.json"))
        self.assertEqual(cfg.mindmap_css_file, str(self.root / "mine.css"))
        self.assertEqual(cfg.warnings, [], "文件存在时不该有告警")

    def test_missing_style_file_warns(self):
        self.write("config.toml", """
[account]
uid = 7

[mindmap]
css_file = "nope.css"
""")
        cfg = load_config(root=self.root).config
        self.assertTrue(any("nope.css" in w for w in cfg.warnings), cfg.warnings)

    def test_cli_override_style_wins_and_is_trimmed(self):
        cfg = load_config(root=self.root,
                          overrides={"uid": 7, "mindmap_style": " pastel "}).config
        self.assertEqual(cfg.mindmap_style, "pastel")

    def test_cli_override_style_is_validated(self):
        with self.assertRaises(ConfigError):
            load_config(root=self.root, overrides={"uid": 7, "mindmap_style": "cute"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
