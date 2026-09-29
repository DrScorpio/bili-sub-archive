"""阶段 0 文档断言 × 证据一致性核对。

用途：防止 `docs/stage0-platform-verification.md` 里的数字/结论与
`stage0/evidence/` 中的实际抓取结果发生漂移。

用法（在项目根目录）：python tools/stage0/check_evidence.py
退出码：0 = 全部一致；1 = 存在不一致。
"""

from __future__ import annotations

import json
import pathlib
import re

EV = pathlib.Path("stage0/evidence")
RAW = EV / "raw"
DOC = pathlib.Path("docs/stage0-platform-verification.md")

summary = json.loads((EV / "summary.json").read_text(encoding="utf-8"))
doc = DOC.read_text(encoding="utf-8")
c = summary["comparison"]
groups = summary["by_group"]

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))


def rec_of(group: str, pid: str) -> dict:
    return next((r for r in groups.get(group, []) if r["probe_id"] == pid), {})


def obs_of(group: str, pid: str) -> str:
    return " | ".join(rec_of(group, pid).get("observations", []))


# ---------------------------------------------------------------- 规模
check("探针数 65", summary["probe_count"] == 65, f"实际 {summary['probe_count']}")

# ------------------------------------------------- 视频：列表与充电权限
check("公开投稿总数 211", c["video_public_total"] == 211, str(c["video_public_total"]))
check("充电专属总数 88", c["video_charging_total"] == 88, str(c["video_charging_total"]))
check("公开列表 p1 = 30", c["video_public_page1_count_auth"] == 30)
check("公开 p1 中充电条目 = 24", c["video_public_page1_charging_items"] == 24)
check("两列表 p1 交集 = 25", c["overlap_bvid_page1"] == 25)

auth_o = obs_of("C2 视频详情", "charging_playurl__auth")
anon_o = obs_of("C2 视频详情", "charging_playurl__anon_buvid")
check("充电 playurl：授权 dash 12 路", "dash 视频流 = 12" in auth_o, auth_o[:60])
check("充电 playurl：匿名 durl 1 段试看", "durl 分段 = 1" in anon_o, anon_o[:60])
check("wbi playurl 恒 412", "HTTP 412" in obs_of("C2 视频详情", "playurl_wbi_variant"))

mp = json.loads((RAW / "multipart_sample.json").read_text(encoding="utf-8"))
check("多 P 样本 4 P", mp["videos"] == 4 and len(mp["pages"]) == 4,
      f"videos={mp['videos']} pages={len(mp['pages'])}")

# ------------------------------------------------- 字幕
cov = json.loads((RAW / "subtitle_coverage.json").read_text(encoding="utf-8"))
zh = [r for r in cov if r.get("has_zh")]
check("字幕覆盖 10/10 有中文", len(zh) == 10 and len(cov) == 10, f"{len(zh)}/{len(cov)}")
check("字幕需登录（匿名 0 条）",
      "字幕条目数 = 0" in obs_of("C2 字幕", "subtitle_charging__anon_buvid"))
check("字幕文件 CDN 匿名可下载",
      rec_of("C2 字幕", "subtitle_file").get("item_count") == 900,
      str(rec_of("C2 字幕", "subtitle_file").get("item_count")))

# ------------------------------------------------- 动态
check("opus 授权/匿名 id 交集 = 20", c["opus_feed_intersection"] == 20)
check("opus badge 授权 19 / 匿名 0",
      c["opus_feed_badged_auth"] == 19 and c["opus_feed_badged_anon"] == 0)
check("充电图文详情对匿名被门控",
      "MODULE_TYPE_BLOCKED" in obs_of("D 动态", "opus_detail__anon_buvid"))
check("动态类型覆盖 4 种", rec_of("D 动态", "dynamic_shapes").get("item_count") == 4,
      str(rec_of("D 动态", "dynamic_shapes").get("item_count")))

# ------------------------------------------------- 专栏（纠正后的结论）
check("计数接口只给 count=44，不返回条目",
      "没有 articles 字段" in obs_of("E 专栏", "article_list__auth")
      and "count = 44" in obs_of("E 专栏", "article_list__auth"))

total = rec_of("E 专栏", "article_opus_feed_total")
check("type=article 翻页得 44 条且与 count 一致",
      total.get("item_count") == 44
      and "两者是否一致 = True" in " ".join(total.get("observations", [])),
      " ".join(total.get("observations", []))[:120])

check("type=article 与 type=all 不重叠（交集 0）",
      rec_of("E 专栏", "article_vs_all_disjoint").get("item_count") == 0)

od_obs = obs_of("E 专栏", "article_opus_detail__auth")
check("用户样例：type=1 / article_type=4 / 181 段 / 5698 字",
      "item.type = 1" in od_obs and "article_type = 4" in od_obs
      and "paragraphs = 181" in od_obs and "正文长度 = 5698" in od_obs, od_obs[:160])

od_anon = obs_of("E 专栏", "article_opus_detail__anon_buvid")
check("充电专属专栏正文对匿名被门控（0 段 / 0 字）",
      "paragraphs = 0" in od_anon and "正文长度 = 0" in od_anon, od_anon[:160])

# 专栏两种格式并存：对照作者走旧格式（articles[]），本项目样本走新格式
smp = rec_of("E 专栏", "article_list_sample")
smp_obs = " ".join(smp.get("observations", []))
check("对照作者旧格式返回 articles[]（旧格式接口可用）",
      "articles" in smp_obs and "articles = 0" not in smp_obs, smp_obs[:160])
check("对照作者也有新格式 opus 专栏（两格式并存）",
      rec_of("E 专栏", "article_opus_feed_sample").get("item_count") == 20,
      str(rec_of("E 专栏", "article_opus_feed_sample").get("item_count")))

# 全量清单：44 篇、43 篇充电专属
inv = json.loads((RAW / "article_opus_inventory.json").read_text(encoding="utf-8"))
inv_ok = [r for r in inv if r["code"] == 0]
check("专栏清单 44 篇全部取到", len(inv) == 44 and len(inv_ok) == 44,
      f"{len(inv_ok)}/{len(inv)}")
check("专栏 43/44 为充电专属",
      len([r for r in inv_ok if r["is_only_fans"]]) == 43,
      str(len([r for r in inv_ok if r["is_only_fans"]])))
check("专栏最长正文 > 12000 字（阶段 3 必须分块）",
      max((r["text_len"] for r in inv_ok), default=0) > 12000,
      str(max((r["text_len"] for r in inv_ok), default=0)))

# ------------------------------------------------- 错误码
errs = {r["probe_id"]: r["code"] for r in groups.get("F 错误码", [])}
check("已删除稿件 = 62002", errs.get("err_deleted_video") == 62002,
      str(errs.get("err_deleted_video")))
check("不存在 aid = -404", errs.get("err_nonexistent_aid") == -404)
check("不存在专栏 = -404", errs.get("err_nonexistent_article") == -404)
check("非法 bvid = -400", errs.get("err_bad_bvid") == -400)
check("Cookie 失效 = -101", errs.get("err_deleted_video") != -101)

# ------------------------------------------------- 文档引用的关键结论
for token in ["211", "88", "62002", "paddlepaddle", "HTTP 412", "ai-zh", "is_charging_arc",
              "MODULE_TYPE_BLOCKED", "rich_text_nodes", "type=article", "article_type",
              "count=44", "has_more", "181", "5,698", "paragraphs"]:
    check(f"文档提及 {token!r}", token in doc)

# ------------------------------------------------- 凭据不落盘
leaks: list[tuple[str, str]] = []
pat = re.compile(r"(SESSDATA|bili_jct|DedeUserID|buvid3|buvid4|bili_ticket)=([^;\"\s]{8,})")
for f in EV.rglob("*"):
    if f.is_file():
        for m in pat.finditer(f.read_text(encoding="utf-8", errors="replace")):
            leaks.append((f.name, m.group(1)))
check("证据目录无凭据值", not leaks, str(leaks[:5]))

# ------------------------------------------------- 输出
failed = [x for x in checks if not x[1]]
for name, ok, detail in checks:
    print(("PASS  " if ok else "FAIL  ") + name + (f"   <- {detail}" if detail and not ok else ""))
print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
raise SystemExit(1 if failed else 0)
