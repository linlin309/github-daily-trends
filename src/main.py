"""主流程编排：

采集 → 预过滤 → GitHub API 补全 → 规则评分 → 分类 → Trend Score
→ 多样性选择 → LLM 单次分析 → 生成 MD/JSON → 落盘 → 发送邮件

约定（与方案一致）：
- 先落盘、再发邮件：SMTP 失败不会丢失当天历史。
- 同一天重复运行：文件名由日期唯一决定（覆盖），已存在则默认跳过邮件（--force 可重发）。
- 单个项目失败不影响整份日报；任一来源失败都能降级；全部失败则以非 0 退出并告警。
- 所有时间判断都基于配置的 IANA 时区，绝不使用 utcnow().date()。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from .analyze import AnalysisResult, LLMAnalyzer
from .classify import Classifier
from .config import ConfigError, load_config
from .filtering import DROP, KEEP, GRAY, SoftwareFilter
from .github_api import GithubClient, build_search_queries, dedupe_into, needs_search
from .history import HistoryStore
from .mailer import Mailer
from .net import HttpClient
from .render import ReportRenderer, dump_json
from .scoring import TrendScorer
from .select import DiversitySelector
from .models import Candidate
from .trending import TrendingCollector, collect_from_fixtures
from .util import local_now

LOG = logging.getLogger("main")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GitHub 每日软件趋势日报生成器")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（相对 --root）")
    parser.add_argument("--root", default=".", help="项目根目录，reports/ 与 prompts/ 相对它解析")
    parser.add_argument("--date", default=None, help="指定报告日期 YYYY-MM-DD（默认按配置时区取当天）")
    parser.add_argument("--force", action="store_true", help="当天已生成过也重新生成并重发邮件")
    parser.add_argument("--dry-run", action="store_true", help="输出到 preview/ 且不发邮件（本地预览用）")
    parser.add_argument("--offline", action="store_true", help="只用本地 fixture 跑通流程，不访问网络")
    parser.add_argument("--demo-ai", action="store_true", help="不调用 LLM，用示例文案预览排版")
    parser.add_argument("--no-email", action="store_true", help="生成报告但不发邮件")
    parser.add_argument(
        "--send-only",
        action="store_true",
        help="只发送邮件：读取已落盘（通常已 commit）的 reports/YYYY-MM-DD.json 重建邮件",
    )
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    # 正式运行要求凭据齐备（fail fast）；dry-run 允许缺凭据，便于本地预览排版。
    cfg = load_config(root / args.config, logger=LOG, strict_env=not args.dry_run)
    if args.dry_run and cfg.get("_meta", {}).get("missing_env"):
        LOG.warning(
            "dry-run 模式：以下环境变量未设置，将被跳过或降级 → %s",
            ", ".join(cfg["_meta"]["missing_env"]),
        )

    tz = cfg.get("timezone", "Asia/Shanghai")
    now = local_now(tz)
    today = date.fromisoformat(args.date) if args.date else now.date()
    date_str = today.isoformat()

    reports_dir = root / cfg["paths"]["reports_dir"]
    report_md = reports_dir / cfg["paths"]["markdown_name"].format(date=date_str)
    already_existed = report_md.is_file()

    degraded: list[str] = []
    if args.date and today != now.date():
        degraded.append(f"补跑模式：报告日期为 {date_str}，但 Trending 只能取到当前实时数据")

    max_description_chars = int(cfg["report"].get("max_description_chars", 300))
    http = HttpClient(
        cfg["http"]["user_agent"],
        timeout_connect=float(cfg["http"].get("timeout_connect", 10)),
        timeout_read=float(cfg["http"].get("timeout_read", 30)),
        max_retries=int(cfg["http"].get("max_retries", 3)),
        backoff_seconds=float(cfg["http"].get("backoff_seconds", 2)),
        logger=LOG,
    )

    def fail(subject: str, body: str, exit_code: int = 1) -> int:
        LOG.error(body)
        if not args.dry_run and not args.no_email:
            _send_alert(cfg, subject, body)
        _write_step_summary(f"## ❌ {subject}\n\n{body}\n")
        return exit_code

    # ---------------------------------------------------------------- 1. 采集
    LOG.info("=== 1/8 采集候选池（时区 %s，报告日期 %s）===", tz, date_str)
    if args.offline:
        LOG.info("离线模式：只使用 tests/fixtures 里的样本")
        candidates, sources_ok, sources_failed = collect_from_fixtures(
            root / "tests" / "fixtures", cfg["sources"], LOG, max_description_chars=max_description_chars
        )
        trending_failed = not sources_ok
    else:
        candidates, sources_ok, sources_failed = TrendingCollector(
            http, cfg["sources"], LOG, max_description_chars=max_description_chars
        ).collect()
        trending_failed = "daily" not in sources_ok

    if sources_failed:
        degraded.append("部分来源采集失败：" + "; ".join(sources_failed[:3]))
    if not candidates:
        return fail(
            f"【GitHub 每日软件趋势】{date_str} 采集失败",
            "Trending 与候选池均为空，今日无法生成日报。原因见 Actions 日志（可能是页面结构变化或网络异常）。",
        )
    LOG.info("候选池：%d 个项目（成功来源 %s）", len(candidates), ", ".join(sources_ok) or "无")

    # ---------------------------------------------------------------- 2. 预过滤
    LOG.info("=== 2/8 规则预过滤（无需 API）===")
    software_filter = SoftwareFilter(cfg["filter"], LOG)
    kept: list = []
    for candidate in candidates:
        result = software_filter.prefilter(candidate)
        software_filter.apply(candidate, result)
        if result.status == DROP:
            continue
        kept.append(candidate)
    LOG.info("预过滤后剩余 %d 个（淘汰 %d 个明显的非软件内容）", len(kept), len(candidates) - len(kept))
    if not kept:
        return fail(
            f"【GitHub 每日软件趋势】{date_str} 无合格项目",
            "所有候选项目在第一轮规则过滤中都被判定为非软件内容，未生成日报。",
        )

    # ---------------------------------------------------------------- 3. 补全
    LOG.info("=== 3/8 GitHub API 数据补全 ===")
    github = GithubClient(
        http,
        cfg["github"],
        token=os.environ.get("GITHUB_TOKEN"),
        logger=LOG,
        max_description_chars=max_description_chars,
    )
    search_cfg = cfg["sources"]["search"]
    weights = cfg["sources"]["weights"]
    used_search = False
    searched_reasons: list[str] = []

    def maybe_search(kept_count: int, phase: str) -> int:
        """Search 兜底：只在候选不足 / Trending 失败 / 过滤后合格项不足时调用。"""
        nonlocal used_search
        if args.offline:
            return 0
        do_search, reason = needs_search(
            candidate_count=len(candidates),
            kept_count=kept_count,
            trending_failed=trending_failed,
            search_cfg=search_cfg,
        )
        if not do_search:
            return 0
        added_total = 0
        for name, query in build_search_queries(search_cfg, today):
            extra = github.search_repositories(
                query,
                sort=next((q.get("sort", "stars") for q in search_cfg["queries"] if q.get("name") == name), "stars"),
                per_page=int(search_cfg.get("per_page", 30)),
                weights=weights,
                max_description_chars=max_description_chars,
            )
            added_total += dedupe_into(candidates, extra, weights)
            used_search = True
        if added_total:
            searched_reasons.append(f"{phase}:{reason}")
            degraded.append(f"已启用 Search 兜底（{phase}：{reason}），新增 {added_total} 个候选")
            LOG.info("Search 兜底新增 %d 个候选（%s / %s）", added_total, phase, reason)
        return added_total

    maybe_search(len(kept), "prefilter")

    if not args.offline:
        github.check_rate_limit()
    plan = github.enrich_plan(kept)
    kept.sort(key=lambda c: (-(c.stars_today or 0), -(c.stars or 0), c.best_rank or 999))
    to_enrich, skipped = kept[:plan], kept[plan:]
    LOG.info("计划补全 %d 个项目（跳过 %d 个）", len(to_enrich), len(skipped))
    for candidate in to_enrich:
        if args.offline:
            break
        github.enrich(candidate, filter_cfg=cfg["filter"])
    if skipped:
        degraded.append(f"{len(skipped)} 个项目未做 API 补全（超出本次额度上限）")

    # ---------------------------------------------------------------- 4. 终筛
    LOG.info("=== 4/8 规则终筛（software_score）===")
    for candidate in kept:
        result = software_filter.finalize(candidate, today=today)
        software_filter.apply(candidate, result)

    qualified = [c for c in kept if c.software_status in (KEEP, GRAY)]
    LOG.info(
        "合格项目 %d 个（keep %d / gray %d / drop %d）",
        len(qualified),
        sum(1 for c in kept if c.software_status == KEEP),
        sum(1 for c in kept if c.software_status == GRAY),
        sum(1 for c in kept if c.software_status == DROP),
    )

    if len(qualified) < int(search_cfg.get("trigger_min_kept", 5)):
        new_count = maybe_search(len(qualified), "finalized")
        if new_count:
            for candidate in candidates[-new_count:]:
                software_filter.apply(candidate, software_filter.prefilter(candidate))
            for candidate in candidates[-new_count:]:
                if candidate.software_status != DROP and not args.offline:
                    github.enrich(candidate, filter_cfg=cfg["filter"])
                software_filter.apply(candidate, software_filter.finalize(candidate, today=today))
                if candidate.software_status in (KEEP, GRAY):
                    qualified.append(candidate)
            LOG.info("Search 兜底并补全后，合格项目增至 %d 个", len(qualified))

    if not qualified:
        return fail(
            f"【GitHub 每日软件趋势】{date_str} 无合格项目",
            "所有候选项目都未通过软件相关性筛选，未生成日报（宁缺毋滥）。",
        )

    # ---------------------------------------------------------------- 5. 打分
    LOG.info("=== 5/8 分类 + Trend Score ===")
    classifier = Classifier(cfg["classify"])
    for candidate in qualified:
        classifier.apply(candidate)

    history = HistoryStore(reports_dir, LOG).load(today, lookback_days=60)
    scorer = TrendScorer(cfg["scoring"])
    for candidate in qualified:
        scorer.compute(
            candidate,
            history=history,
            today=today,
            repeat_lookback_days=int(cfg["selection"].get("repeat_lookback_days", 3)),
        )

    # ---------------------------------------------------------------- 6. 选择
    LOG.info("=== 6/8 多样性选择（热度优先 + 软惩罚）===")
    selector = DiversitySelector(cfg["selection"])
    outcome = selector.select(
        qualified,
        target=int(cfg["report"]["target_count"]),
        floor=float(cfg["report"]["hard_floor"]),
    )
    LOG.info(
        "选中 %d 个项目（上限 %d，停止原因 %s）",
        outcome.count,
        cfg["report"]["target_count"],
        outcome.stopped_reason,
    )
    for candidate in outcome.selected:
        LOG.info(
            "  %02d %-40s 热度 %5.1f 调整后 %5.1f  %s/%s",
            candidate.rank,
            candidate.full_name,
            candidate.trend_score,
            candidate.adjusted_score or 0.0,
            candidate.group,
            candidate.category,
        )

    if not outcome.selected:
        return fail(
            f"【GitHub 每日软件趋势】{date_str} 无项目达到质量下限",
            f"合格项目 {len(qualified)} 个，但热度分都低于质量下限 "
            f"{cfg['report']['hard_floor']}，今日不生成日报（宁缺毋滥）。",
        )

    # ---------------------------------------------------------------- 7. AI
    LOG.info("=== 7/8 AI 分析（计划 1 次调用）===")
    analyzer = LLMAnalyzer(cfg["llm"], http, logger=LOG, project_root=root)
    analysis = analyzer.analyze(outcome.selected, today=today, demo=args.demo_ai)
    if not analysis.ok:
        degraded.append(f"AI 分析不可用（{analysis.degraded_reason}），本次为纯数据日报")
        LOG.warning("AI 分析失败：%s", analysis.degraded_reason)
    else:
        LOG.info("AI 分析完成：provider=%s model=%s calls=%d", analysis.provider, analysis.model, analysis.calls)
    if analyzer.calls > int(cfg["llm"].get("max_calls_total", 2)):
        degraded.append(f"LLM 调用次数 {analyzer.calls} 超出预期上限")

    # ---------------------------------------------------------------- 8. 落盘
    LOG.info("=== 8/8 生成报告并落盘 ===")
    mode = "search-only" if (trending_failed and used_search) else ("trending+search" if used_search else "trending")
    renderer = ReportRenderer(cfg, project_root=root)
    bundle = renderer.render(
        report_date=today,
        selected=outcome.selected,
        candidates=candidates,
        analysis=analysis,
        meta={
            "mode": mode,
            "generated_at": now.isoformat(timespec="seconds"),
            "sources_ok": sources_ok,
            "sources_failed": sources_failed,
            "degraded": degraded,
            "kept_count": len(qualified),
            "stopped_reason": outcome.stopped_reason,
            "floor": cfg["report"]["hard_floor"],
            "weights_version": cfg["scoring"].get("weights_version"),
        },
    )

    out_dir = root / "preview" if args.dry_run else reports_dir
    written = _write_outputs(out_dir, date_str, bundle, dry_run=args.dry_run)
    for role, path in written.items():
        LOG.info("已写入 %s: %s", role, path.relative_to(root) if path.is_relative_to(root) else path)

    # ---------------------------------------------------------------- 邮件
    email_result = None
    should_send = (not args.dry_run) and (not args.no_email) and (args.force or not already_existed)
    if should_send:
        mailer = Mailer(cfg["mail"], logger=LOG)
        LOG.info("发送邮件到 %s", mailer.describe_target())
        email_result = mailer.send_report(
            subject=f"{cfg['report']['title_prefix']}{date_str}",
            html=bundle.html,
            plain_text=bundle.plain_text,
        )
        if email_result.ok:
            LOG.info("邮件发送成功（尝试 %d 次）", email_result.attempts)
        else:
            LOG.error("邮件发送失败：%s", email_result.error)
            degraded.append(f"邮件发送失败：{email_result.error}（报告已保存，不影响历史）")
    elif args.dry_run:
        LOG.info("dry-run：跳过邮件发送，预览文件在 preview/ 目录")
    elif args.no_email:
        LOG.info("--no-email：跳过邮件发送")
    else:
        LOG.info("报告 %s 已存在，跳过重复发送邮件（如需重发请使用 --force）", date_str)

    _write_step_summary(
        _summary_markdown(date_str, outcome, analysis, mode, degraded, written, email_result)
        + (("\n\n---\n\n### 预览报告（dry-run）\n\n" + bundle.markdown) if args.dry_run else "")
    )
    LOG.info("完成：精选 %d 个项目，mode=%s", outcome.count, mode)
    return 0


def run_send_only(args: argparse.Namespace, *, root: Path, cfg: dict) -> int:
    """只发送邮件：从已落盘的报告 JSON 重建邮件。

    GitHub Actions 里的顺序是：生成（--no-email）→ commit/push → 本模式发送邮件，
    以保证"先保存历史，再发邮件"。
    """
    tz = cfg.get("timezone", "Asia/Shanghai")
    date_str = args.date or local_now(tz).date().isoformat()
    json_path = root / cfg["paths"]["reports_dir"] / cfg["paths"]["json_name"].format(date=date_str)
    if not json_path.is_file():
        LOG.error("找不到报告文件 %s，无法发送邮件", json_path)
        return 1

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    selected = [Candidate.from_dict(item) for item in payload.get("selected") or []]
    if not selected:
        LOG.error("报告 %s 中没有任何项目，跳过发送", date_str)
        return 1

    llm = payload.get("llm") or {}
    counts = payload.get("counts") or {}
    selection = payload.get("selection") or {}
    sources = payload.get("sources") or {}
    analysis = AnalysisResult(
        ok=bool(llm.get("ok")),
        provider=llm.get("provider"),
        model=llm.get("model"),
        calls=int(llm.get("calls") or 0),
        degraded_reason=llm.get("degraded_reason"),
        summary=payload.get("summary") or {},
        tokens=llm.get("tokens") or {},
        demo=bool(llm.get("demo")),
        notes=list(llm.get("notes") or []),
    )
    bundle = ReportRenderer(cfg, project_root=root).render(
        report_date=date.fromisoformat(date_str),
        selected=selected,
        candidates=[],
        analysis=analysis,
        meta={
            "mode": payload.get("mode"),
            "generated_at": payload.get("generated_at"),
            "degraded": payload.get("degraded") or [],
            "stopped_reason": selection.get("stopped_reason"),
            "floor": selection.get("floor"),
            "sources_ok": sources.get("ok") or [],
            "sources_failed": sources.get("failed") or [],
            "candidates_count": counts.get("candidates"),
            "kept_count": counts.get("after_filter"),
            "weights_version": payload.get("weights_version"),
        },
    )

    mailer = Mailer(cfg["mail"], logger=LOG)
    LOG.info("发送邮件到 %s（报告 %s，%d 个项目）", mailer.describe_target(), date_str, len(selected))
    result = mailer.send_report(
        subject=f"{cfg['report']['title_prefix']}{date_str}",
        html=bundle.html,
        plain_text=bundle.plain_text,
    )
    if result.ok:
        LOG.info("邮件发送成功（尝试 %d 次）", result.attempts)
        _write_step_summary(
            f"## ✅ 邮件已发送\n\n- 报告：`reports/{date_str}.md`\n"
            f"- 收件人：{', '.join(cfg['mail']['to'])}\n- 项目数：{len(selected)}\n"
        )
        return 0

    LOG.error("邮件发送失败：%s", result.error)
    _write_step_summary(f"## ❌ 邮件发送失败\n\n```\n{result.error}\n```\n")
    return 1


# ---------------------------------------------------------------------- 辅助
def _write_outputs(out_dir: Path, date_str: str, bundle, *, dry_run: bool) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    md_path = out_dir / f"{date_str}.md"
    md_path.write_text(bundle.markdown, encoding="utf-8")
    written["markdown"] = md_path

    json_path = out_dir / f"{date_str}.json"
    json_path.write_text(dump_json(bundle.json_payload), encoding="utf-8")
    written["json"] = json_path

    if dry_run:
        html_path = out_dir / f"{date_str}.html"
        html_path.write_text(bundle.html, encoding="utf-8")
        written["html"] = html_path
        txt_path = out_dir / f"{date_str}.txt"
        txt_path.write_text(bundle.plain_text, encoding="utf-8")
        written["plain_text"] = txt_path
    return written


def _send_alert(cfg: dict, subject: str, body: str) -> None:
    try:
        Mailer(cfg["mail"], logger=LOG).send_alert(subject=subject, text=body)
        LOG.info("已发送告警邮件")
    except Exception as exc:  # noqa: BLE001 - 告警失败不能掩盖原始错误
        LOG.error("告警邮件发送失败: %s", exc)


def _write_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    except OSError as exc:
        LOG.warning("写入 GITHUB_STEP_SUMMARY 失败: %s", exc)


def _summary_markdown(date_str, outcome, analysis, mode, degraded, written, email_result) -> str:
    lines = [f"## GitHub 每日软件趋势 {date_str}", ""]
    lines.append(f"- 精选 **{outcome.count}** 个项目（模式：{mode}，停止原因：{outcome.stopped_reason}）")
    lines.append(f"- AI：{'成功' if analysis.ok else '降级'} provider={analysis.provider} calls={analysis.calls}")
    if email_result is not None:
        lines.append(f"- 邮件：{'成功' if email_result.ok else '失败 - ' + str(email_result.error)}")
    lines.append("")
    lines.append("| # | 项目 | 热度分 | 分类 | 今日增长 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for c in outcome.selected:
        growth = "—" if c.stars_today is None else f"+{c.stars_today:,}"
        lines.append(f"| {c.rank} | [{c.full_name}]({c.url}) | {c.trend_score:.1f} | {c.category} | {growth} |")
    if degraded:
        lines.append("")
        lines.append("**运行提示**")
        for item in degraded:
            lines.append(f"- {item}")
    lines.append("")
    names = ", ".join(f"{k}={v.name}" for k, v in written.items())
    lines.append(f"_产物：{names}_")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    try:
        if args.send_only:
            root = Path(args.root).resolve()
            return run_send_only(args, root=root, cfg=load_config(root / args.config, logger=LOG))
        return run(args)
    except ConfigError as exc:
        LOG.error("配置错误：%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.error("被中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
