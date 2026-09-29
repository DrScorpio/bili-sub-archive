"""交付契约测试：模板/文档/CLI/版本/仓库卫生，防止"代码改了文档没改"。

这些用例不测业务逻辑，只锁**交付面**：

1. ``config.example.toml`` 覆盖全部配置字段（新增配置项忘了写模板 = 测试失败）；
2. README 里出现的每一个 ``bsa`` 命令行开关都真实存在（文档不漂移）；
3. 版本号在 ``pyproject.toml`` 与 ``bili_sub_archive.__version__`` 之间一致；
4. 退出码契约与错误分类映射固定；
5. 依赖安装提示覆盖全部被探测的依赖；
6. 仓库里没有真实凭据、``.gitignore`` 覆盖凭据与产物目录。

v0.1.0 发布时，需求/方案/阶段记录与当时的验收工具曾移入 ``docs/archive/``；
这些原始过程记录与工具后来已从仓库移除，结论收拢到 ``docs/DEVELOPMENT.md``。
原先覆盖那些工具的用例随之删除——它们测的是不再随仓库维护的代码。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import tomllib
import unittest

from bili_sub_archive import FORMAT_VERSION, __version__
from bili_sub_archive.cli import _steps_from, build_parser
from bili_sub_archive.config import Config, load_config
from bili_sub_archive.deps import INSTALL_HINTS, probe_dependencies
from bili_sub_archive.errors import (
    EXIT_AUTH,
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_RISK_STOP,
    EXIT_UNEXPECTED,
    ConfigError,
    CredentialError,
    RiskControlStop,
    exit_code_for_kind,
)
from tests import ROOT, drop_workspace, make_workspace, strip_config_env

README = ROOT / "README.md"
TEMPLATE = ROOT / "config.example.toml"

#: 配置字段 → 模板里的 ``(段, 键)``。新增配置项时必须同时更新模板，否则下面的用例失败。
FIELD_TO_TEMPLATE: dict[str, tuple[str, str]] = {
    "uid": ("account", "uid"),
    "output_dir": ("output", "dir"),
    "download_images": ("output", "download_images"),
    "image_workers": ("output", "image_workers"),
    "date_from": ("filter", "from"),
    "date_to": ("filter", "to"),
    "latest": ("filter", "latest"),
    "interval_seconds": ("request", "interval_seconds"),
    "timeout_seconds": ("request", "timeout_seconds"),
    "retries": ("request", "retries"),
    "max_pages": ("request", "max_pages"),
    "user_agent": ("request", "user_agent"),
    "scan_charging_video_subset": ("discovery", "scan_charging_video_subset"),
    "scan_dynamic_secondary_source": ("discovery", "scan_dynamic_secondary_source"),
    "probe_video_access": ("discovery", "probe_video_access"),
    "download_media": ("download", "media"),
    "video_workers": ("download", "video_workers"),
    "segment_workers": ("download", "segment_workers"),
    "video_quality": ("download", "quality"),
    "video_timeout_seconds": ("download", "timeout_seconds"),
    "ffmpeg_path": ("download", "ffmpeg_path"),
    "ffprobe_path": ("download", "ffprobe_path"),
    "prefer_subtitle": ("transcript", "prefer_subtitle"),
    "asr_enabled": ("asr", "enabled"),
    "asr_model": ("asr", "model"),
    "asr_device": ("asr", "device"),
    "asr_compute_type": ("asr", "compute_type"),
    "asr_language": ("asr", "language"),
    "asr_beam_size": ("asr", "beam_size"),
    "asr_keep_audio": ("asr", "keep_audio"),
    "render_dynamic_png": ("render", "dynamic_png"),
    "render_width": ("render", "width"),
    "render_max_height": ("render", "max_height"),
    "render_font": ("render", "font"),
    "summary_enabled": ("summary", "enabled"),
    "summary_base_url": ("summary", "base_url"),
    "summary_model": ("summary", "model"),
    "summary_client": ("summary", "client"),
    "summary_prompt_file": ("summary", "prompt_file"),
    "summary_chunk_chars": ("summary", "chunk_chars"),
    "summary_max_chunks": ("summary", "max_chunks"),
    "summary_overlap_chars": ("summary", "overlap_chars"),
    "summary_temperature": ("summary", "temperature"),
    "summary_max_tokens": ("summary", "max_tokens"),
    "summary_timeout_seconds": ("summary", "timeout_seconds"),
    "summary_retries": ("summary", "retries"),
    "summary_min_chars": ("summary", "min_chars"),
    "mindmap_enabled": ("mindmap", "enabled"),
    "mindmap_mmdc_path": ("mindmap", "mmdc_path"),
    "mindmap_puppeteer_config": ("mindmap", "puppeteer_config"),
    "mindmap_max_nodes": ("mindmap", "max_nodes"),
    "mindmap_max_depth": ("mindmap", "max_depth"),
    "mindmap_label_chars": ("mindmap", "label_chars"),
    "mindmap_width": ("mindmap", "width"),
    "mindmap_background": ("mindmap", "background"),
    "mindmap_style": ("mindmap", "style"),
    "mindmap_font_size": ("mindmap", "font_size"),
    "mindmap_font_family": ("mindmap", "font_family"),
    "mindmap_config_file": ("mindmap", "config_file"),
    "mindmap_css_file": ("mindmap", "css_file"),
    "mindmap_timeout_seconds": ("mindmap", "timeout_seconds"),
    "lock_stale_hours": ("storage", "lock_stale_hours"),
}

#: 不算配置字段的元信息（不进模板）与内容类型开关（模板用 [scope] 三行表示）
META_FIELDS = frozenset({"config_files", "warnings", "kinds"})

#: 模板里允许出现、但不对应 Config 字段的键（凭据与别名）
TEMPLATE_EXTRA_KEYS: dict[str, set[str]] = {
    "account": {"cookie", "mid"},
    "summary": {"api_key", "llm_api_key"},
}

SECTION_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\s*$")
KEY_RE = re.compile(r"^\s*#?\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")


def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def template_sections() -> dict[str, str]:
    """把模板按 ``[段]`` 切成文本块（含注释行）。"""
    out: dict[str, list[str]] = {}
    current = ""
    for line in template_text().splitlines():
        match = SECTION_RE.match(line.strip())
        if match:
            current = match.group("name")
            out.setdefault(current, [])
            continue
        if current:
            out[current].append(line)
    return {name: "\n".join(lines) for name, lines in out.items()}


class ConfigTemplateTest(unittest.TestCase):
    """配置模板：可解析、覆盖全部字段、只有占位符。"""

    def test_template_parses_as_toml(self):
        data = tomllib.loads(template_text())
        self.assertIn("account", data)
        self.assertIn("summary", data)
        self.assertIn("mindmap", data)

    def test_template_covers_every_config_field(self):
        sections = template_sections()
        missing: list[str] = []
        for field, (section, key) in FIELD_TO_TEMPLATE.items():
            block = sections.get(section)
            if block is None or not re.search(rf"^\s*#?\s*{re.escape(key)}\s*=", block, re.M):
                missing.append(f"{field} → [{section}] {key}")
        self.assertEqual(missing, [], f"配置模板缺少这些键：{missing}")

    def test_template_has_no_unknown_keys(self):
        """模板里不该出现代码不认识的键（拼错的键会静默失效）。"""
        sections = template_sections()
        known: dict[str, set[str]] = {}
        for _field, (section, key) in FIELD_TO_TEMPLATE.items():
            known.setdefault(section, set()).add(key)
        known["scope"] = {"dynamic", "video", "article"}
        for section, extra in TEMPLATE_EXTRA_KEYS.items():
            known.setdefault(section, set()).update(extra)
        unknown: list[str] = []
        for section, block in sections.items():
            for line in block.splitlines():
                match = KEY_RE.match(line)
                if match and match.group("key") not in known.get(section, set()):
                    unknown.append(f"[{section}] {match.group('key')}")
        self.assertEqual(unknown, [], f"模板里有代码不识别的键：{unknown}")

    def test_template_has_no_credential_values(self):
        text = template_text()
        self.assertNotRegex(text, r"SESSDATA=[A-Za-z0-9%_,-]{20,}")
        self.assertNotRegex(text, r"sk-[A-Za-z0-9_-]{20,}")
        for line in text.splitlines():
            if line.strip().startswith("#"):
                continue
            self.assertNotIn("cookie", line.lower(), f"模板出现了未注释的 cookie：{line}")

    def test_template_loads_with_documented_defaults(self):
        saved = strip_config_env()
        root = make_workspace("delivery")
        try:
            (root / "config.toml").write_text(template_text(), encoding="utf-8")
            loaded = load_config(root=root)
            cfg = loaded.config
            self.assertEqual(cfg.uid, 123456789)          # 模板里的占位 UID
            self.assertEqual(cfg.enabled_kinds, ["dynamic", "video", "article"])
            self.assertAlmostEqual(cfg.interval_seconds, 1.2)
            self.assertEqual(cfg.video_workers, 3)
            self.assertEqual(cfg.segment_workers, 4)
            self.assertEqual(cfg.summary_chunk_chars, 6000)
            self.assertEqual(cfg.mindmap_max_nodes, 60)
            self.assertFalse(cfg.asr_enabled)              # ASR 默认关闭（需求 6.7）
            self.assertFalse(loaded.credentials.has_cookie)  # 模板里没有凭据
        finally:
            drop_workspace(root)
            os.environ.update(saved)

    def test_every_config_field_is_mapped(self):
        fields = {f for f in Config.__dataclass_fields__ if not f.startswith("_")}
        unmapped = sorted(fields - set(FIELD_TO_TEMPLATE) - META_FIELDS)
        self.assertEqual(unmapped, [], f"这些配置字段没有登记到模板映射：{unmapped}")


class ReadmeContractTest(unittest.TestCase):
    """README：存在、结构齐备、命令行开关与代码一致。"""

    def setUp(self):
        self.text = README.read_text(encoding="utf-8")

    def test_readme_has_required_sections(self):
        for heading in ("## 2. 系统要求", "## 3. 安装", "## 4. 配置", "## 5. 使用",
                        "## 6. 输出结构", "## 7. 退出码", "## 8. 已知限制",
                        "## 9. 隐私与凭据", "## 10. 故障排查", "## 11. 自检与验收"):
            self.assertIn(heading, self.text, f"README 缺少 {heading}")

    def test_readme_covers_all_external_components(self):
        for token in ("3.11", "yt-dlp", "FFmpeg", "Node.js", "mermaid-cli",
                      "faster-whisper", "Pillow", "openai", "winget", "npm install"):
            self.assertIn(token, self.text, f"安装说明没有覆盖 {token}")

    def test_readme_documents_all_exit_codes(self):
        for code in (0, 1, 2, 3, 4, 5, 130):
            self.assertRegex(self.text, rf"\|\s*{code}\s*\|", f"退出码 {code} 未记录")

    def test_readme_flags_exist_in_cli(self):
        """README 里每条 ``bsa <子命令>`` 命令用到的开关都必须被该子命令接受。"""
        parser = build_parser()
        main_flags = {s for action in parser._actions for s in action.option_strings}
        sub = next(action for action in parser._actions
                   if isinstance(action, argparse._SubParsersAction))
        per_cmd = {name: {s for action in sub_parser._actions
                          for s in action.option_strings}
                   for name, sub_parser in sub.choices.items()}
        unknown: list[str] = []
        for line in self.text.splitlines():
            # 文档路径行（开发文档索引等）不属于产品 CLI 面，跳过
            if "bsa " not in line or "docs/" in line:
                continue
            tokens = line.split("bsa ", 1)[1].split()
            command = next((t for t in tokens if t in per_cmd), None)
            if command is None:
                continue
            valid = main_flags | per_cmd[command]
            for flag in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", line):
                if flag not in valid:
                    unknown.append(f"{command} {flag}（{line.strip()[:60]}）")
        self.assertEqual(unknown, [], f"README 用到子命令不接受的开关：{unknown}")

    def test_readme_documents_retry_and_asr_semantics(self):
        self.assertIn("retry", self.text)
        self.assertIn("默认关闭", self.text)          # ASR 默认关闭
        self.assertIn("no_transcript", self.text)     # 无文字稿的显式状态
        self.assertIn("llm_not_configured", self.text)
        self.assertIn("不提供本地视频画面 OCR", self.text)

    def test_readme_relative_links_resolve(self):
        """README 里的相对链接必须指向真实存在的文件（归档/改名后路径不漂移）。"""
        broken: list[str] = []
        for target in re.findall(r"\]\(([^)\s]+?)\)", self.text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if not (ROOT / target.split("#", 1)[0]).exists():
                broken.append(target)
        self.assertEqual(broken, [], f"README 链接指向不存在的路径：{broken}")

    def test_readme_points_at_development_doc(self):
        """开发文档必须是 ``docs/DEVELOPMENT.md``，且仓库根目录不再散落过程文档。"""
        self.assertTrue((ROOT / "docs" / "DEVELOPMENT.md").is_file())
        self.assertIn("docs/DEVELOPMENT.md", self.text)
        # 原始过程记录（文档 + 验证工具）已从仓库移除；只留本机不入库的脱敏样本
        self.assertFalse((ROOT / "docs" / "archive" / "README.md").exists())
        self.assertFalse((ROOT / "docs" / "archive" / "tools").exists())
        # 仓库根目录不再散落开发过程文档
        for stray in ("DEVELOPMENT_PLAN.md", "REQUIREMENTS.md"):
            self.assertFalse((ROOT / stray).exists(), f"{stray} 不应留在仓库根目录")
        self.assertFalse((ROOT / "tools").exists(), "tools/ 不应留在仓库根目录")


class CliSurfaceTest(unittest.TestCase):
    """命令行面：子命令、全局开关位置、退出码常量。"""

    def test_subcommands_exist(self):
        parser = build_parser()
        choices = next(action.choices for action in parser._actions
                       if isinstance(action, argparse._SubParsersAction))
        self.assertEqual(set(choices), {"check", "sync", "retry"})

    def test_global_flags_work_before_and_after_subcommand(self):
        parser = build_parser()
        before = parser.parse_args(["--json", "sync", "--uid", "1"])
        after = parser.parse_args(["sync", "--uid", "1", "--json"])
        default = parser.parse_args(["sync", "--uid", "1"])
        self.assertTrue(before.json)
        self.assertTrue(after.json)
        self.assertFalse(default.json)
        self.assertEqual(parser.parse_args(["sync", "--uid", "1", "--config", "x.toml"]).config,
                         "x.toml")
        self.assertEqual(parser.parse_args(["check", "--uid", "1", "-v"]).verbose, 1)

    def test_color_and_progress_flags_work_before_and_after_subcommand(self):
        """终端呈现开关与 ``--json``/``-v`` 同规格：写在子命令前后都生效。"""
        parser = build_parser()
        for argv in (["--color=never", "sync", "--uid", "1"],
                     ["sync", "--uid", "1", "--color=never"]):
            self.assertEqual(parser.parse_args(argv).color, "never")
        for argv in (["--no-progress", "check"], ["check", "--no-progress"]):
            self.assertTrue(parser.parse_args(argv).no_progress)
        # 不写值的 --color 等价于 always（约定见 README 5.6）
        self.assertEqual(parser.parse_args(["sync", "--uid", "1", "--color"]).color, "always")
        self.assertEqual(parser.parse_args(["sync", "--uid", "1"]).color, "auto")
        self.assertFalse(parser.parse_args(["retry", "--uid", "1"]).no_progress)

    def test_version_flag_exits_zero(self):
        from bili_sub_archive.cli import main

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(__version__, stream.getvalue())

    def test_retry_accepts_config_override_flags(self):
        """``retry`` 必须能表达"配置变更"：装好 mmdc / 开启 ASR 后只补做那一步。"""
        args = build_parser().parse_args(
            ["retry", "--uid", "1", "--steps", "mindmap,transcript",
             "--mmdc", "C:/npm/mmdc.cmd", "--asr", "--no-subtitle",
             "--summary-model", "m", "--summary-base-url", "https://api.example.com/v1",
             "--mindmap-style", "dark"])
        self.assertEqual(args.steps, "mindmap,transcript")
        self.assertEqual(args.mindmap_mmdc_path, "C:/npm/mmdc.cmd")
        self.assertEqual(args.mindmap_style, "dark")
        self.assertTrue(args.asr_enabled)
        self.assertFalse(args.prefer_subtitle)
        self.assertEqual(args.summary_model, "m")
        # 筛选类开关属于 sync：retry 不接受（避免"补做时又改选条目范围"的歧义）
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["retry", "--uid", "1", "--latest", "5"])

    def test_unknown_step_is_config_error(self):
        with self.assertRaises(ConfigError):
            _steps_from(argparse.Namespace(steps="summary,nope"))

    def test_step_filter_accepts_known_steps(self):
        steps = _steps_from(argparse.Namespace(steps="summary, mindmap,media"))
        self.assertEqual(steps, {"summary", "mindmap", "media"})

    def test_exit_code_constants_are_stable(self):
        self.assertEqual((EXIT_OK, EXIT_UNEXPECTED, EXIT_CONFIG, EXIT_AUTH,
                          EXIT_PARTIAL, EXIT_RISK_STOP), (0, 1, 2, 3, 4, 5))
        self.assertEqual(ConfigError.exit_code, 2)
        self.assertEqual(CredentialError.exit_code, 3)
        self.assertEqual(RiskControlStop.exit_code, 5)

    def test_error_kind_maps_to_documented_exit_codes(self):
        self.assertEqual(exit_code_for_kind("not_found"), EXIT_CONFIG)
        self.assertEqual(exit_code_for_kind("bad_request"), EXIT_CONFIG)
        self.assertEqual(exit_code_for_kind("auth_invalid"), EXIT_AUTH)
        self.assertEqual(exit_code_for_kind("network_error"), EXIT_AUTH)


class VersionContractTest(unittest.TestCase):
    def test_pyproject_matches_package_version(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["version"], __version__)

    def test_format_version_is_locked(self):
        """产物结构版本：新增字段一律附加，因此 ``FORMAT_VERSION`` 保持 3。"""
        self.assertEqual(FORMAT_VERSION, 3)


class DependencyHintTest(unittest.TestCase):
    def test_every_probed_dependency_has_an_install_hint(self):
        keys = {dep.key for dep in probe_dependencies()}
        missing = sorted(keys - set(INSTALL_HINTS))
        self.assertEqual(missing, [], f"这些依赖没有安装提示：{missing}")

    def test_hints_are_actionable(self):
        for key, hint in INSTALL_HINTS.items():
            self.assertTrue(hint.strip(), f"{key} 的安装提示为空")
            self.assertRegex(hint, r"pip install|npm install|winget install|PATH",
                             f"{key} 的安装提示不可操作：{hint}")

    def test_optional_dependencies_are_marked(self):
        optional = {dep.key for dep in probe_dependencies() if dep.optional}
        self.assertEqual(optional, {"openai", "rich"})

    def test_missing_ignores_optional(self):
        from bili_sub_archive.deps import Dependency, missing

        deps = [Dependency(key="a", label="A", purpose="", available=False),
                Dependency(key="openai", label="openai", purpose="", available=False,
                           optional=True)]
        self.assertEqual([d.key for d in missing(deps)], ["a"])
        self.assertEqual({d.key for d in missing(deps, include_optional=True)}, {"a", "openai"})


class RepoHygieneTest(unittest.TestCase):
    """仓库卫生：没有真实凭据，产物目录都已忽略。"""

    SCAN_FILES = ("README.md", "config.example.toml", "pyproject.toml",
                  "docs/DEVELOPMENT.md")

    def test_gitignore_covers_credentials_and_artifacts(self):
        lines = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8")
                 .splitlines() if line.strip() and not line.startswith("#")}
        for pattern in ("config.local.toml", "require.txt", "output/", ".test_tmp/",
                        ".smoke_sync/", "__pycache__/"):
            self.assertIn(pattern, lines, f".gitignore 缺少 {pattern}")

    def test_no_real_credentials_in_delivery_files(self):
        hits: list[str] = []
        for name in self.SCAN_FILES:
            text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
            for _ in re.finditer(r"SESSDATA=([A-Za-z0-9%_,-]{20,})", text):
                hits.append(f"{name}: SESSDATA=…")
            for _ in re.finditer(r"\bsk-[A-Za-z0-9_-]{20,}", text):
                hits.append(f"{name}: sk-…")
        self.assertEqual(hits, [], f"交付文件里出现疑似真实凭据：{hits}")

    def test_local_config_is_ignored_if_present(self):
        local = ROOT / "config.local.toml"
        if local.exists():
            ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("config.local.toml", ignored)

    def test_stage4_artifacts_are_ignored(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".test_tmp/", ignored)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
