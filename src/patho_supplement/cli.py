"""运维命令行。

本产品没有图形界面 —— 签发交互在院内病理系统里完成，日常操作（批处理重放、
凭据自检、挂起清单处理）走命令行。每个子命令都有明确的退出码：``0`` 成功，
非 ``0`` 失败，方便挂到运维脚本与 CI 上。
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from .align.lexicon import load_lexicon
from .align.matcher import MATCH_LEVEL_LABELS, MatchThresholds
from .compose.baseline import BaselineError
from .models import MatchLevel
from .compose.pathology_client import PathologyClient, PathologyClientError
from .ingest.er7 import Er7ParseError
from .replay import run_replay

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
#: 已经有另一个守护进程在盯这个目录了 —— 本进程不启动（不是故障，是互斥）。
EXIT_ALREADY_RUNNING = 5


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="patho-supplement",
        description="病理补充报告生成系统 · 运维命令行",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="子命令")

    replay = subparsers.add_parser(
        "replay",
        help="批处理重放：把结果目录里的回报跑成草稿与待人工处理清单",
    )
    replay.add_argument(
        "--inbox",
        required=True,
        help="结果投递目录（默认取环境变量 PATHOLOGY_RESULT_INBOX）",
    )
    replay.add_argument(
        "--baseline",
        default=None,
        help="病理报告基线快照（默认取结果目录下的 baseline.json）",
    )
    replay.add_argument(
        "--out",
        default="data/replay",
        help="产物输出目录（默认 data/replay）",
    )
    replay.add_argument(
        "--state-db",
        default=None,
        help="状态库路径；给了则跨批次去重（守护进程用的就是这种模式）",
    )
    replay.add_argument(
        "--lexicon",
        default=None,
        help="部位同义词典文件（默认取环境变量 PATHOLOGY_LEXICON_PATH；缺省用内置词表）",
    )
    replay.set_defaults(func=cmd_replay)

    daemon = subparsers.add_parser(
        "daemon",
        help="守护常驻：盯着结果投递目录，回报到达即处理并回写病理系统",
    )
    daemon.add_argument(
        "--inbox",
        default=None,
        help="结果投递目录（默认取环境变量 PATHOLOGY_RESULT_INBOX）",
    )
    daemon.add_argument(
        "--state-db",
        default=None,
        help="状态库路径（默认取环境变量 PATHOLOGY_STATE_DB）",
    )
    daemon.add_argument(
        "--out",
        default="data/daemon",
        help="产物输出目录（草稿与待人工处理清单，默认 data/daemon）",
    )
    daemon.add_argument(
        "--lock-file",
        default=None,
        help="单实例锁文件路径（默认放在状态库同级）",
    )
    daemon.add_argument(
        "--fallback-interval",
        type=float,
        default=None,
        help=(
            "定时兜底扫描的间隔（秒）：结果目录长时间没新文件时按这个周期强制扫一遍，"
            "接住停机期间投进来的文件（默认取环境变量 WATCH_FALLBACK_INTERVAL）"
        ),
    )
    daemon.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮就退出（演练与 CI 用）",
    )
    daemon.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="最多跑多少轮（不指定则一直跑到收到停止信号）",
    )
    daemon.add_argument(
        "--log-level",
        default=None,
        help="结构化行日志级别（DEBUG/INFO/WARNING/ERROR，默认取环境变量 LOG_LEVEL）",
    )
    daemon.add_argument(
        "--log-file",
        default=None,
        help="行日志落盘文件（默认取环境变量 LOG_FILE；不配置则写标准错误）",
    )
    daemon.set_defaults(func=cmd_daemon)

    doctor = subparsers.add_parser(
        "doctor",
        help="自检：凭据位、对外接口清单与出站连通性",
    )
    doctor.add_argument(
        "--accession",
        default=None,
        help="指定病理号则真取一次基线，验证出站接口可用",
    )
    doctor.set_defaults(func=cmd_doctor)

    pending = subparsers.add_parser(
        "pending",
        help="挂起项处理：列出待人工处理的条目、按补齐结果从挂起点续跑",
    )
    pending_sub = pending.add_subparsers(dest="pending_command", metavar="子命令")

    pending_list = pending_sub.add_parser(
        "list",
        help="列出待人工处理的挂起项（谁没对上、缺哪个字段、候选是谁）",
    )
    pending_list.add_argument(
        "--state-db",
        default=None,
        help="状态库路径（默认取环境变量 PATHOLOGY_STATE_DB）",
    )
    pending_list.add_argument(
        "--accession",
        default=None,
        help="只看某一个病理号的挂起项",
    )
    pending_list.set_defaults(func=cmd_pending_list)

    pending_resolve = pending_sub.add_parser(
        "resolve",
        help="补齐挂起项缺的字段，并从挂起的那一步续跑（命中就并入草稿新版本）",
    )
    pending_resolve.add_argument(
        "--id",
        type=int,
        required=True,
        help="挂起行 ID（pending list 里那一行的编号）",
    )
    pending_resolve.add_argument(
        "--set",
        dest="fills",
        action="append",
        default=[],
        metavar="字段名=值",
        help=(
            "补齐的字段与值，可重复给出（如 --set block_no=B1 --set region_desc=宫颈）。"
            "只接受缺失字段，不接受整条结果重投"
        ),
    )
    pending_resolve.add_argument(
        "--state-db",
        default=None,
        help="状态库路径（默认取环境变量 PATHOLOGY_STATE_DB）",
    )
    pending_resolve.add_argument(
        "--baseline",
        default=None,
        help=(
            "本地基线快照文件（不给则从病理系统实时取基线，"
            "需要 PATHOLOGY_BASE_URL 与 PATHOLOGY_API_TOKEN）"
        ),
    )
    pending_resolve.add_argument(
        "--out",
        default="data/pending",
        help="草稿文件输出目录（默认 data/pending）",
    )
    pending_resolve.set_defaults(func=cmd_pending_resolve)

    pending_export = pending_sub.add_parser(
        "export",
        help="导出可带走的待人工处理清单（CSV 可排序筛选 + 文本可打印）",
        description=(
            "导出可带走的待人工处理清单：CSV（能进 Excel 排序筛选）与"
            "文本（能直接打印）两种文件。导出不带患者可识别明细。"
        ),
    )
    pending_export.add_argument(
        "--state-db",
        default=None,
        help="状态库路径（默认取环境变量 PATHOLOGY_STATE_DB）",
    )
    pending_export.add_argument(
        "--out",
        default="data/pending-export",
        help="导出目录（默认 data/pending-export）",
    )
    pending_export.add_argument(
        "--accession",
        default=None,
        help="只导出某一个病理号的挂起项",
    )
    pending_export.set_defaults(func=cmd_pending_export)
    # 「pending」是一组子命令：只敲 pending 不带子命令时，把这一组的用法打出来。
    parser.set_defaults(pending_parser=pending)

    audit = subparsers.add_parser(
        "audit",
        help="审计流水：导出某个病理号的来龙去脉（复核、内审、飞检核对用）",
    )
    audit_sub = audit.add_subparsers(dest="audit_command", metavar="子命令")

    audit_export = audit_sub.add_parser(
        "export",
        help="导出审计流水（CSV 可排序筛选 + 文本可打印）",
        description=(
            "导出审计流水：CSV（能进 Excel 排序筛选）与文本（能直接打印）"
            "两种文件。件头写明行数口径，导出不带患者可识别明细。"
        ),
    )
    audit_export.add_argument(
        "--state-db",
        default=None,
        help="状态库路径（默认取环境变量 PATHOLOGY_STATE_DB）",
    )
    audit_export.add_argument(
        "--accession",
        default=None,
        help="只导出某一个病理号的审计流水（不给则导出全部）",
    )
    audit_export.add_argument(
        "--out",
        default="data/audit-export",
        help="导出目录（默认 data/audit-export）",
    )
    audit_export.set_defaults(func=cmd_audit_export)
    parser.set_defaults(audit_parser=audit)

    benchmark = subparsers.add_parser(
        "benchmark",
        help="基准集跑批：跑产品真路径并做三方对照，输出对齐率 / 错绑定数等指标",
        description=(
            "把一份基准集（基线 + 入站报文 + 手写标准答案）跑过产品真路径，"
            "逐条对账后给出指标：错绑定数、误并入数、同分多候选挂起率、"
            "无第三去向；并在同一份输入上跑两种院内常见做法（仅规范化精确匹配 / "
            "仅取词法最高分）做三方对照。指标或对照判定不达标即以非 0 退出，"
            "可直接挂到 CI 上。"
        ),
    )
    benchmark.add_argument(
        "--dataset",
        default="tests/fixtures/benchmark",
        help=(
            "基准集目录（须含 baseline.json 与 ground_truth.json；"
            "默认 tests/fixtures/benchmark）"
        ),
    )
    benchmark.add_argument(
        "--inbox",
        default=None,
        help=(
            "结果目录（替代 --dataset 作基准集根）：目录里放报文与 baseline.json，"
            "标准答案用 --ground-truth 指定或放同目录 ground_truth.json"
        ),
    )
    benchmark.add_argument(
        "--baseline",
        default=None,
        help="基线快照路径（默认取基准集目录里的 baseline.json）",
    )
    benchmark.add_argument(
        "--ground-truth",
        default=None,
        help="标准答案路径（默认取基准集目录里的 ground_truth.json）",
    )
    benchmark.add_argument(
        "--out",
        default="reports/benchmark",
        help="指标与产物输出目录（默认 reports/benchmark）",
    )
    benchmark.add_argument(
        "--state-db",
        default=None,
        help="状态库路径；给了则审计流水也落库（用于核对「依据一条不落」）",
    )
    benchmark.add_argument(
        "--scale-report",
        default=None,
        help=(
            "规模与性能验收报告：把 --inbox 目录里的全部报文跑过产品真路径，"
            "给出端到端耗时 / 单轮对齐率 / 峰值内存占用 / 状态库体积四项"
            "可度量指标并按阈值判定，报告写到这个文件。给了这个参数时"
            "不做标准答案对账（规模集没有手写标准答案）"
        ),
    )
    benchmark.add_argument(
        "--scale-work-dir",
        default="data/scale",
        help="规模验收的产物目录（草稿、运行摘要、状态库；默认 data/scale）",
    )
    benchmark.set_defaults(func=cmd_benchmark)

    summary = subparsers.add_parser(
        "summary",
        help="运行摘要：看某一轮跑了多少（从运行产物目录或状态库读）",
        description=(
            "打印一轮运行的摘要：从运行产物目录读（replay / daemon 写出的 "
            "summary.json），或从状态库汇总（草稿、挂起项、回写队列、"
            "版本冲突、检查项状态）。两个来源都给就都打印。"
        ),
    )
    summary.add_argument(
        "--dir",
        dest="run_dir",
        default=None,
        help="运行产物目录（读该目录下的 summary.json）",
    )
    summary.add_argument(
        "--state-db",
        default=None,
        help="状态库路径（默认取环境变量 PATHOLOGY_STATE_DB）",
    )
    summary.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="以 JSON 输出（供其他程序读取）",
    )
    summary.set_defaults(func=cmd_summary)

    return parser


def cmd_replay(args: argparse.Namespace) -> int:
    """执行批处理重放。"""
    import os

    from .config import Config, ConfigError
    from .db import Database
    from .logging_setup import build_round_summary, setup_logging

    database = None
    if args.state_db:
        database = Database(args.state_db)
        database.init_schema()

    lexicon_path = args.lexicon or os.environ.get("PATHOLOGY_LEXICON_PATH")

    # 多路召回与归属绑定的运行参数走配置面（过滤策略 / 并发上限 / 单路超时 /
    # 判定阈值）。配置越界说明有人把参数写错了 —— 本轮直接不启动，并把是哪一项、
    # 合法范围是多少说清楚；悄悄退回默认值会让人以为「按配置跑过了」。
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"配置有误，本轮不启动：{exc}", file=sys.stderr)
        return EXIT_FAILURE
    fanout_kwargs: dict[str, object] = {
        "filter_policy": config.filter_policy,
        "fanout_max_workers": config.fanout_max_workers,
        "fanout_route_timeout": config.fanout_route_timeout,
        "thresholds": MatchThresholds.from_config(config),
    }
    # 结构化行日志：批处理重放也按同一套口径记一行摘要，
    # 运维不必分「守护跑的」和「重放跑的」两套看法。
    logger = setup_logging(
        level=config.log_level,
        path=config.log_file,
        include_result_values=config.log_result_values,
    )

    try:
        started = time.monotonic()
        outcome = run_replay(
            args.inbox,
            baseline_path=args.baseline,
            output_dir=args.out,
            database=database,
            lexicon_path=lexicon_path,
            **fanout_kwargs,
        )
    except (BaselineError, Er7ParseError) as exc:
        print(f"重放失败：{exc}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        if database is not None:
            database.close()

    print("== 病理补充报告 · 批处理重放 ==")
    print(f"结果目录：{outcome.inbox}")
    print(f"基线来源：{outcome.baseline_source}")
    print(
        f"报文文件：{outcome.files_scanned} 个"
        f"（结果 {outcome.result_files} / 申请 {outcome.order_files}）"
    )
    print(
        f"回报结果：{outcome.parsed_results} 条 → 入账 {outcome.accepted} 条"
        f"（重复投递 {outcome.duplicates} 条，更正取代 {outcome.superseded} 条）"
    )
    if outcome.corrections_applied or outcome.inverted_corrections:
        print(
            f"更正：生效 {outcome.corrections_applied} 条"
            f"；回报时间倒挂未生效 {outcome.inverted_corrections} 条"
        )
    if outcome.withheld:
        print(
            f"无法写入报告：{outcome.withheld} 条"
            f"（无法获得 {outcome.not_obtained}、值为空 {outcome.empty_value}）"
            f"→ 已进待人工处理清单"
        )
    print(
        f"草稿：{len(outcome.drafts)} 份，共 {outcome.draft_entry_count} 条"
        f"；待人工处理：{len(outcome.pending)} 条"
    )
    if outcome.placeholder_count:
        # 已申请但一次都没回来的检查项 —— 草稿里以「待回报」占位，医师看得见。
        print(
            f"待回报占位：{outcome.placeholder_count} 项"
            f"（已申请、这一版还没回来，草稿里占一行「待回报」）"
        )
    levels = outcome.match_levels or {}
    if levels:
        breakdown = "、".join(
            f"{MATCH_LEVEL_LABELS.get(key, key)} {levels.get(key, 0)} 条"
            for key in (
                MatchLevel.L1_BLOCK_EXACT.value,
                MatchLevel.L2_REGION_NORMALIZED.value,
                MatchLevel.L3_REGION_FUZZY.value,
                MatchLevel.L4_PENDING.value,
            )
        )
        print(f"对齐判定：{breakdown}")
    print(
        f"对齐依据：{outcome.rationale_count} 条"
        f"（挂起 {outcome.pending_rationale_count} 条）"
    )
    if outcome.draft_entry_count + len(outcome.pending) != outcome.accepted - outcome.superseded:
        print(
            "警告：入账条数与草稿 + 待处理条数对不上，请检查对齐环节",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    # 一行结构化摘要（与守护进程同一套口径）。
    summary = build_round_summary(outcome)
    summary.elapsed_seconds = time.monotonic() - started
    logger.event("replay_summary", **summary.as_fields())

    if outcome.deferred:
        print(
            f"申请侧事件：{len(outcome.deferred)} 个文件，解析出申请 "
            f"{outcome.parsed_orders} 条"
        )
    print(
        f"待查项：{outcome.derived_queries} 项（在等结果 {outcome.awaiting_queries}、"
        f"取消 {outcome.cancelled_queries}、暂停 {outcome.held_queries}、"
        f"终止 {outcome.terminated_queries}）；检索键 {outcome.recall_key_count} 把"
    )
    fanout = outcome.fanout or {}
    if fanout:
        print(
            f"多路召回：{fanout.get('routes_total', 0)} 路"
            f"（成功 {fanout.get('routes_ok', 0)}、失败 {fanout.get('routes_failed', 0)}、"
            f"超时 {fanout.get('routes_timed_out', 0)}），"
            f"命中 {fanout.get('items', 0)} 条"
            f"（并发上限 {fanout.get('max_workers', 0)}、"
            f"单路超时 {fanout.get('route_timeout', 0)} 秒）"
        )
        print(
            f"候选合并：合并 {fanout.get('merged', 0)} 条 → 去重后 "
            f"{fanout.get('kept', 0)} 条（去掉重复 {fanout.get('duplicates', 0)} 条）"
        )
        for failure in fanout.get("failures", []) or []:
            print(
                f"  - 召回失败 {failure.get('route_id')}："
                f"{failure.get('error')}（{failure.get('error_type')}）",
                file=sys.stderr,
            )
    if outcome.order_rejected:
        print(f"拒收的申请条目：{outcome.order_malformed} 条")
        for name, reason in outcome.order_rejected:
            print(f"  - {name}：{reason}")
    if outcome.rejected:
        print(f"拒收文件：{len(outcome.rejected)} 个")
        for name, reason in outcome.rejected:
            print(f"  - {name}：{reason}")

    print("产物：")
    for draft, path in zip(outcome.drafts, outcome.draft_paths):
        print(f"  草稿    {path}（{draft.accession_no}，{draft.merged_count} 条）")
    if outcome.pending_path is not None:
        print(f"  待处理  {outcome.pending_path}")

    lexicon = outcome.lexicon or {}
    print(
        f"部位同义词典：版本 {lexicon.get('version', '-')}"
        f"，条目 {lexicon.get('entry_count', 0)}"
        f"（别名 {lexicon.get('alias_count', 0)}）"
        f"，来源 {lexicon.get('source', '-')}"
    )
    for warning in lexicon.get("warnings", []) or []:
        print(f"  词典告警：{warning}", file=sys.stderr)

    return EXIT_OK


#: 对外接口清单：入站 2 条事件流 + 出站 2 条 HTTP + 推送 1 条。
EXTERNAL_INTERFACES: tuple[tuple[str, str, str, str], ...] = (
    (
        "入站",
        "ORU^R01 结果事件流",
        "结果投递目录下的落盘文件（免疫组化室 / 分子实验室投递）",
        "PATHOLOGY_RESULT_INBOX",
    ),
    (
        "入站",
        "ORM^O01 申请事件流",
        "同上（决定哪些检查项在等结果）",
        "PATHOLOGY_RESULT_INBOX",
    ),
    (
        "出站",
        "GET 病理报告基线",
        "GET {PATHOLOGY_BASE_URL}/api/v1/pathology-reports/{病理号}",
        "PATHOLOGY_BASE_URL + PATHOLOGY_API_TOKEN",
    ),
    (
        "出站",
        "POST 补充报告草稿",
        "POST {PATHOLOGY_BASE_URL}/api/v1/pathology-reports/{病理号}/supplemental-drafts",
        "PATHOLOGY_BASE_URL + PATHOLOGY_API_TOKEN",
    ),
    (
        "推送",
        "SMTP / 企业微信通知",
        "成稿后通知当班医师（未开通时可降级，不阻塞成稿）",
        "SMTP_* + WECOM_WEBHOOK_URL",
    ),
)


def cmd_daemon(args: argparse.Namespace) -> int:
    """启动守护进程（常驻）。"""
    from dataclasses import replace

    from .config import Config, ConfigError
    from .daemon import Daemon, SingleInstanceLock
    from .logging_setup import setup_logging

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"配置有误，守护进程不启动：{exc}", file=sys.stderr)
        return EXIT_FAILURE
    if args.inbox:
        config = replace(
            config, pathology_result_inbox=Path(args.inbox).expanduser().resolve()
        )
    if args.state_db:
        config = replace(
            config, pathology_state_db=Path(args.state_db).expanduser().resolve()
        )
    if getattr(args, "fallback_interval", None) is not None:
        interval = float(args.fallback_interval)
        if interval <= 0:
            print(
                f"配置有误，守护进程不启动：--fallback-interval 必须大于 0，"
                f"当前值 {interval}",
                file=sys.stderr,
            )
            return EXIT_FAILURE
        config = replace(config, watch_fallback_interval=interval)

    missing = config.missing_required()
    if missing:
        print(
            "缺少必需的环境变量：" + "、".join(missing) + "（参考 .env.example 填写）",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    lock_path = _lock_file_path(args, config)
    lock = SingleInstanceLock(lock_path)
    if not lock.acquire():
        # 已经有另一个守护进程在盯这个目录了。两个实例会互相抢文件、抢回写，
        # 所以本进程直接不启动 —— 并把「谁占着、锁在哪、怎么办」说清楚。
        holder = lock.holder_pid()
        print(
            f"已有一个守护进程在运行，本次不启动。\n"
            f"锁文件：{lock_path}\n"
            f"占用进程号：{holder}\n"
            f"处理建议：先确认那个进程确实停掉了（它可能是 systemd 托管的服务）；"
            f"若两个实例要盯不同的结果目录，请用 --lock-file 各自指定锁文件。",
            file=sys.stderr,
        )
        _record_lock_conflict(config, lock_path=lock_path, holder_pid=holder)
        return EXIT_ALREADY_RUNNING

    daemon = None
    try:
        # 结构化行日志：一行一件事，运维看日志就知道这一轮干了什么。
        # 结果值默认不入日志（LOG_RESULT_VALUES 显式打开才放行）。
        setup_logging(
            level=args.log_level or config.log_level,
            path=args.log_file or config.log_file,
            include_result_values=config.log_result_values,
        )
        daemon = Daemon(config, output_dir=args.out)
        # 中断恢复：把上一轮留下的挂起项、幂等键、回写队列、目录里的待处理
        # 报文接回来，并报一行可核对的清单（重启之后最该问的一句）。
        daemon.recover()
        if args.once:
            # 单轮模式：先等报文落稳（目录监听默认不读还在写的文件），
            # 再跑一轮，避免刚投进来的回报被这一轮漏掉。
            settle = max(0.0, config.watch_settle_seconds)
            if settle:
                time.sleep(settle)
            daemon.run_once()
            return EXIT_OK

        stopping = {"flag": False}

        def _handle_stop(signum, frame):  # noqa: ARG001 - 信号处理固定签名
            stopping["flag"] = True
            print("\n收到停止信号，本轮处理完即退出。")

        signal.signal(signal.SIGINT, _handle_stop)
        signal.signal(signal.SIGTERM, _handle_stop)

        print(
            f"守护进程已启动｜结果目录 {config.pathology_result_inbox}"
            f"｜锁文件 {lock_path}"
            f"｜兜底扫描间隔 {config.watch_fallback_interval:.0f} 秒"
        )
        daemon.run_forever(
            max_cycles=args.cycles,
            fallback_interval=config.watch_fallback_interval,
            stop_requested=lambda: stopping["flag"],
        )
        return EXIT_OK
    except PathologyClientError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        if daemon is not None:
            daemon.close()
        lock.release()


def _lock_file_path(args: argparse.Namespace, config) -> Path:
    """单实例锁文件路径：命令行优先，其次配置面，最后放在状态库同级。

    两个实例盯同一个目录必须共用一把锁；盯不同目录时可以用 ``--lock-file``
    各给一把 —— 这正是「院内机资源紧张，一个目录只跑一个守护进程」的口径。
    """
    override = getattr(args, "lock_file", None)
    if override:
        return Path(override).expanduser()
    if config.daemon_lock_path:
        return Path(config.daemon_lock_path).expanduser()
    return Path(config.pathology_state_db or "data/patho_state.db").parent / (
        "patho_supplement.lock"
    )


def _record_lock_conflict(config, *, lock_path: Path, holder_pid: str) -> None:
    """锁冲突也留一行痕迹（状态库没建起来就算了，不影响退出码）。

    「这个目录今天被启动过几次、每次都被谁占着」是要能查的 —— 重复启动
    是运维最常见的手滑。
    """
    try:
        from .audit.ledger import (
            CATEGORY_RUNTIME,
            EVENT_DAEMON_LOCK_CONFLICT,
            record_audit,
        )
        from .db import Database

        database = Database(config.pathology_state_db or "data/patho_state.db")
        try:
            database.init_schema()
            record_audit(
                database,
                category=CATEGORY_RUNTIME,
                event=EVENT_DAEMON_LOCK_CONFLICT,
                accession_no=None,
                detail={
                    "lock_file": str(lock_path),
                    "holder_pid": holder_pid,
                },
            )
        finally:
            database.close()
    except Exception:  # noqa: BLE001 - 留痕失败不许影响退出码
        pass


def _display_width(text: str) -> int:
    """终端显示宽度（中文按两格算）。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _row_text(cells: tuple[str, ...], widths: tuple[int, ...]) -> str:
    """按显示宽度对齐的一行（中文列不会错位）。"""
    parts: list[str] = []
    for cell, width in zip(cells, widths):
        parts.append(cell + " " * max(1, width - _display_width(cell)))
    return "".join(parts).rstrip()


def cmd_doctor(args: argparse.Namespace) -> int:
    """凭据位、对外接口清单与出站连通性自检。"""
    from .config import Config, ConfigError

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"配置有误，自检不通过：{exc}", file=sys.stderr)
        print("请按上面的键名与合法范围修正后重试。", file=sys.stderr)
        return EXIT_FAILURE
    missing = config.missing_required()

    print("== 病理补充报告 · 运维自检 ==")
    print("")
    print("【凭据位】")
    for key, present in config.credential_status().items():
        print(f"  {key:<26}{'已配置' if present else '未配置'}")

    print("")
    print("【对外接口清单】")
    for index, (direction, name, contract, credential) in enumerate(
        EXTERNAL_INTERFACES, start=1
    ):
        print(f"  {index}. [{direction}] {name}")
        print(f"     契约：{contract}")
        print(f"     凭据：{credential}")

    print("")
    print("【出站连通性】")
    try:
        client = PathologyClient.from_config(config)
    except PathologyClientError as exc:
        print(f"  未建立客户端：{exc.detail}")
    else:
        reachable, detail = client.check_reachable()
        print(f"  TCP 连通：{'可达' if reachable else '不可达'}（{detail}）")
        if args.accession:
            ok, detail = client.check_baseline(args.accession)
            print(f"  基线取数：{'可用' if ok else '失败'}（{detail}）")
        else:
            print("  基线取数：未指定病理号，已跳过（doctor --accession <病理号> 可实测）")

    print("")
    print("【生效阈值与运行参数】")
    header = ("参数", "环境变量", "当前值", "缺省值", "合法范围")
    print("  " + _row_text(header, (12, 26, 10, 10, 0)))
    for row in config.threshold_rows():
        print(
            "  "
            + _row_text(
                (
                    str(row["name"]),
                    str(row["env"]),
                    str(row["value"]),
                    str(row["default"]),
                    str(row["spec"]),
                ),
                (12, 26, 10, 10, 0),
            )
        )

    print("")
    print("【通知通道】")
    from .notify.dispatcher import NotifyDispatcher

    for row in NotifyDispatcher.from_config(config).channel_status():
        state = "已配置" if row["configured"] else "未配置"
        print(f"  {row['label']:<14}{state}  {row['detail']}")

    print("")
    print("【当班收件人路由】")
    from .notify.router import DutyRouter

    router = DutyRouter.from_config(config)
    roster = router.current_roster()
    print(f"  {'排班表':<22}{roster.source or '未配置（NOTIFY_DUTY_ROSTER_PATH）'}")
    print(f"  {'排班表版本':<22}{roster.version or '（无）'}")
    print(f"  {'班次数':<22}{roster.shift_count}")
    for warning in roster.warnings:
        print(f"  告警：{warning}")
    route = router.route()
    print(f"  {'当前收件人':<22}{'、'.join(route.recipients) or '（无）'}")
    print(f"  {'路由依据':<22}{route.basis_label}｜{route.detail}")

    print("")
    print("【部位同义词典】")
    lexicon = load_lexicon(config.lexicon_path)
    print(f"  {'版本':<22}{lexicon.version}")
    print(f"  {'条目数':<22}{lexicon.entry_count}（别名 {lexicon.alias_count}）")
    print(f"  {'来源':<22}{lexicon.source}")
    for warning in lexicon.warnings:
        print(f"  告警：{warning}")

    print("")
    if missing:
        print(
            "缺少必需的环境变量：" + "、".join(missing) + "（参考 .env.example 填写）",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    print("自检通过：凭据位齐备。")
    return EXIT_OK


#: 退出码：补齐后**仍对不上**，挂起继续等人（与「已并入草稿」区分开）。
EXIT_STILL_PENDING = 3
#: 这一行正被别人补齐续跑（互斥没抢到）：等一下重试即可，不是失败。
EXIT_LOCKED = 4


def _state_db_path(args: argparse.Namespace, config) -> Path:
    """状态库路径：命令行优先，其次配置面（默认 ``data/patho_state.db``）。"""
    override = getattr(args, "state_db", None)
    if override:
        return Path(override).expanduser().resolve()
    return Path(config.pathology_state_db or "data/patho_state.db").expanduser().resolve()


def cmd_pending_list(args: argparse.Namespace) -> int:
    """列出待人工处理的挂起项。"""
    from .config import Config, ConfigError
    from .compose.conflict import list_open_conflicts
    from .db import Database
    from .pending.fills import FIELD_LABELS
    from .pending.lock import PendingLock
    from .pending.store import PENDING_STATES, PendingStore

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"配置有误，本轮不启动：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    db_path = _state_db_path(args, config)
    if not db_path.is_file():
        print(
            f"状态库不存在：{db_path}（先跑一轮守护进程，或用 replay --state-db 落库）",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    database = Database(db_path)
    try:
        store = PendingStore(database)
        loaded = store.load_all(states=PENDING_STATES, strict=False)
        lock = PendingLock(database, ttl=config.pending_lock_ttl)
        holders = {
            snapshot.snapshot_id: lock.holder(snapshot.snapshot_id)
            for snapshot in loaded.snapshots
        }
        conflicts = [
            conflict
            for conflict in list_open_conflicts(database)
            if not args.accession or conflict.accession_no == args.accession
        ]
    finally:
        database.close()

    snapshots = [
        snapshot
        for snapshot in loaded.snapshots
        if not args.accession or snapshot.accession_no == args.accession
    ]

    print("== 待人工处理清单 ==")
    print(f"状态库：{db_path}")
    if loaded.broken_rows:
        print(
            f"告警：状态库有 {len(loaded.broken_rows)} 行挂起项读不出来（行号 "
            + "、".join(str(row_id) for row_id in loaded.broken_rows)
            + "）",
            file=sys.stderr,
        )
    if not snapshots:
        print("（当前没有待人工处理的挂起项）")
        if not conflicts:
            return EXIT_OK
    elif snapshots:
        header = ("行ID", "病理号", "项目", "原因", "待补字段", "候选部位", "尝试", "快照时间", "状态")
        widths = (5, 18, 12, 14, 18, 26, 4, 19, 10)
        print("  " + _row_text(header, widths))
        for snapshot in snapshots:
            candidates = (
                "；".join(
                    f"{block.region_desc or '部位未登记'}/{block.block_no or '无蜡块'}"
                    for block in snapshot.candidates
                )
                or "无"
            )
            missing = (
                "、".join(FIELD_LABELS.get(name, name) for name in snapshot.missing_fields)
                or "无"
            )
            if holders.get(snapshot.snapshot_id) is not None:
                # 这一行正被别人补齐续跑：先别抢，等对方跑完。
                state = "处理中"
            elif snapshot.escalated:
                state = "转人工"
            elif snapshot.fill_map:
                state = "补过仍挂起"
            else:
                state = "等人补齐"
            print(
                "  "
                + _row_text(
                    (
                        str(snapshot.snapshot_id),
                        snapshot.accession_no,
                        snapshot.item_code,
                        snapshot.reason.label,
                        missing,
                        candidates,
                        str(snapshot.attempts),
                        snapshot.snapshot_at,
                        state,
                    ),
                    widths,
                )
            )
        print(f"合计：{len(snapshots)} 条待人工处理")

    if conflicts:
        # 版本冲突：整份草稿写不回去，也要人处理，单独列一段。
        print()
        print("== 版本冲突待处置 ==")
        for conflict in conflicts:
            print(
                f"  冲突#{conflict.id}｜{conflict.accession_no}｜"
                f"本地草稿 {conflict.version_text}｜基础版本 "
                f"{conflict.base_version or '（未取到）'}｜{conflict.detected_at}"
            )
            print(f"      原因：{conflict.reason}")
            print(f"      怎么处置：{conflict.hint}")
        print(f"合计：{len(conflicts)} 条版本冲突待处置")
    return EXIT_OK


def cmd_pending_export(args: argparse.Namespace) -> int:
    """导出可带走的待人工处理清单（CSV + 人类可读文本）。"""
    from .config import Config, ConfigError
    from .compose.conflict import list_open_conflicts
    from .db import Database
    from .pending.export import export_pending
    from .pending.store import PENDING_STATES, PendingStore

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"配置有误，本轮不启动：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    db_path = _state_db_path(args, config)
    if not db_path.is_file():
        print(
            f"状态库不存在：{db_path}（先跑一轮守护进程，或用 replay --state-db 落库）",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    database = Database(db_path)
    try:
        store = PendingStore(database)
        loaded = store.load_all(states=PENDING_STATES, strict=False)
        conflicts = [
            conflict
            for conflict in list_open_conflicts(database)
            if not args.accession or conflict.accession_no == args.accession
        ]
    finally:
        database.close()

    records = [
        snapshot.to_record()
        for snapshot in loaded.snapshots
        if not args.accession or snapshot.accession_no == args.accession
    ]

    out_dir = Path(args.out).expanduser().resolve()
    export = export_pending(records, out_dir, conflicts=conflicts)

    print("== 挂起清单导出 ==")
    print(f"状态库：{db_path}")
    if loaded.broken_rows:
        print(
            f"告警：状态库有 {len(loaded.broken_rows)} 行挂起项读不出来（行号 "
            + "、".join(str(row_id) for row_id in loaded.broken_rows)
            + "）",
            file=sys.stderr,
        )
    print(f"CSV：{export.csv_path}")
    print(f"文本：{export.text_path}")
    print(f"条目数：{export.row_count} 条")
    if export.conflict_count:
        print(f"版本冲突待处置：{export.conflict_count} 条（已随清单导出）")
    if export.reason_counts:
        print(
            "原因分布："
            + "；".join(f"{label} {count} 条" for label, count in export.reason_counts)
        )
    return EXIT_OK


def cmd_audit_export(args: argparse.Namespace) -> int:
    """导出审计流水（CSV + 人类可读文本）。"""
    from .audit.export import export_audit
    from .audit.ledger import audit_counts, read_audit_rows
    from .config import Config, ConfigError
    from .db import Database

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"配置有误，本轮不启动：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    db_path = _state_db_path(args, config)
    if not db_path.is_file():
        print(
            f"状态库不存在：{db_path}（先跑一轮守护进程，或用 replay --state-db 落库）",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    database = Database(db_path)
    try:
        rows = read_audit_rows(database, accession_no=args.accession)
        counts = audit_counts(database, accession_no=args.accession)
    finally:
        database.close()

    out_dir = Path(args.out).expanduser().resolve()
    export = export_audit(rows, out_dir, counts=counts, accession_no=args.accession)

    print("== 审计流水导出 ==")
    print(f"状态库：{db_path}")
    print(f"范围：{args.accession or '全部病理号'}")
    print(f"CSV：{export.csv_path}")
    print(f"文本：{export.text_path}")
    print(f"导出条数：{export.row_count} 行")
    print(f"行数口径：{counts.summary()}")
    if args.accession:
        # 畸形报文连病理号都解析不出来 —— 它是批次级的记录，不挂在这个报告下。
        print("提示：畸形报文拒收属批次级记录，不随单个病理号导出")
    return EXIT_OK


def _resolve_baseline(args: argparse.Namespace, config, accession_no: str):
    """取这份报告的基线。

    给了 ``--baseline`` 就读本地快照（离线复现、演练用）；不给则**从病理系统
    实时取**（真实出站读，凭据取自环境变量）—— 院内日常走的是这一条。
    """
    from .compose.baseline import BaselineError, load_baseline_file
    from .compose.pathology_client import PathologyClient

    if args.baseline:
        baselines = load_baseline_file(args.baseline)
        baseline = baselines.get(accession_no)
        if baseline is None:
            raise BaselineError(f"基线快照里没有病理号 {accession_no}")
        return baseline, f"本地快照 {args.baseline}"
    client = PathologyClient.from_config(config)
    return client.fetch_baseline(accession_no), "病理系统实时取基线"


def cmd_pending_resolve(args: argparse.Namespace) -> int:
    """补齐挂起项缺的字段，并从挂起的那一步续跑。"""
    from .align.lexicon import load_lexicon
    from .align.matcher import MatchThresholds
    from .align.order_guard import OrderStateGuard
    from .align.query_derivation import PendingQueryStore
    from .compose.draft_store import load_draft_body
    from .config import Config, ConfigError
    from .db import Database
    from .pending.fills import FIELD_LABELS, FillError, parse_fills
    from .pending.integrity import SnapshotIntegrityError
    from .pending.resume import (
        PendingLockedError,
        ResumeError,
        record_fills,
        resume_pending,
    )
    from .pending.store import PendingStore

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"配置有误，本轮不启动：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    try:
        fills = parse_fills(args.fills)
    except FillError as exc:
        print(f"补齐值不合法：{exc.detail}", file=sys.stderr)
        return EXIT_USAGE

    db_path = _state_db_path(args, config)
    if not db_path.is_file():
        print(
            f"状态库不存在：{db_path}（先跑一轮守护进程，或用 replay --state-db 落库）",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    database = Database(db_path)
    try:
        store = PendingStore(database)
        snapshot = store.load_by_id(args.id)
        if snapshot is None:
            print(f"状态库里没有第 {args.id} 行挂起项", file=sys.stderr)
            return EXIT_FAILURE

        try:
            baseline, baseline_source = _resolve_baseline(args, config, snapshot.accession_no)
        except (BaselineError, PathologyClientError) as exc:
            print(f"取基线失败：{exc}", file=sys.stderr)
            return EXIT_FAILURE

        try:
            # 补齐值先登记（不在互斥锁内）：即使这一行正被别人处理，
            # 补的值也已经落在库里，不会丢。
            record_fills(database, pending_id=args.id, fills=fills)
            # 申请侧状态闸门：已取消 / 已终止的待查项，补齐了也不能并入草稿。
            guard = OrderStateGuard.from_queries(
                PendingQueryStore(database).load(snapshot.accession_no)
            )
            outcome = resume_pending(
                database,
                pending_id=args.id,
                baseline=baseline,
                thresholds=MatchThresholds.from_config(config),
                guard=guard,
                max_attempts=config.pending_max_attempts,
                lexicon=load_lexicon(config.lexicon_path),
                lock_ttl=config.pending_lock_ttl,
            )
        except PendingLockedError as exc:
            print(f"行已被处理：{exc}", file=sys.stderr)
            return EXIT_LOCKED
        except (ResumeError, SnapshotIntegrityError) as exc:
            print(f"续跑失败：{exc}", file=sys.stderr)
            return EXIT_FAILURE

        draft_path = None
        if outcome.merged and outcome.draft_id is not None:
            directory = Path(args.out).expanduser() / "drafts"
            directory.mkdir(parents=True, exist_ok=True)
            draft_path = directory / (
                f"{outcome.accession_no}-v{outcome.draft_version}.txt"
            )
            draft_path.write_text(
                load_draft_body(database, outcome.draft_id), encoding="utf-8"
            )
    finally:
        database.close()

    print("== 挂起项补齐与续跑 ==")
    print(f"挂起行：第 {outcome.pending_id} 行｜{outcome.accession_no}｜{outcome.item_code}")
    print(
        "补齐值："
        + "、".join(
            f"{FIELD_LABELS.get(fill.field_name, fill.field_name)}={fill.value}"
            for fill in outcome.fills
        )
    )
    print(f"基线来源：{baseline_source}")
    if outcome.merged:
        from .align.matcher import MATCH_LEVEL_LABELS

        level_label = MATCH_LEVEL_LABELS.get(outcome.match_level or "", outcome.match_level)
        print(
            f"结果：已并入草稿（版本 {outcome.draft_version}，"
            f"共 {outcome.draft_entry_count} 条；"
            f"命中 {level_label}，置信 {outcome.confidence}）"
            + ("　［重复提交：沿用上一次结论，未重复产出草稿］" if outcome.idempotent else "")
        )
        if draft_path is not None:
            print(f"草稿文件：{draft_path}")
        print("草稿已在状态库落库（待回写：回写队列与冲突处置见运维说明）")
        return EXIT_OK

    candidates = (
        "；".join(
            f"{block.region_desc or '部位未登记'}/{block.block_no or '无蜡块'}"
            for block in outcome.candidates
        )
        or "无"
    )
    missing = "、".join(
        FIELD_LABELS.get(name, name) for name in outcome.missing_fields
    ) or "无"
    print(
        f"结果：仍对不上（原因 {outcome.reason}；待补 {missing}；"
        f"尝试 {outcome.attempts} 次"
        + ("，已转人工处置" if outcome.escalated else "")
        + "）"
    )
    print(f"候选部位：{candidates}")
    return EXIT_STILL_PENDING


def cmd_benchmark(args: argparse.Namespace) -> int:
    """基准集跑批：跑产品真路径，逐条对账后输出对齐指标，并与两种朴素做法对照。"""
    import json

    if args.scale_report:
        # 规模验收模式：量「扛不扛得住、涨不涨得凶」，不对账。
        return cmd_scale_report(args)

    from .align.benchmark import load_benchmark, run_benchmark
    from .align.comparison import COMPARISON_JSON_NAME, run_comparison
    from .compose.baseline import BaselineError
    from .db import Database
    from .ingest.er7 import Er7ParseError

    dataset = Path(args.inbox or args.dataset).expanduser()
    try:
        benchmark = load_benchmark(
            dataset,
            baseline_path=args.baseline,
            ground_truth_path=args.ground_truth,
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"基准集不可用：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    database = None
    if args.state_db:
        database = Database(args.state_db)
        database.init_schema()

    try:
        run = run_benchmark(benchmark, output_dir=out_dir, database=database)
    except (BaselineError, Er7ParseError, OSError, ValueError) as exc:
        print(f"基准集跑批失败：{exc}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        if database is not None:
            database.close()

    metrics = run.metrics
    metrics_path = out_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(metrics.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("== 病理补充报告 · 基准集跑批 ==")
    print(f"基准集：{benchmark.root}（版本 {benchmark.version or '-'}）")
    print(f"标准答案：{benchmark.case_count} 条")
    print(f"指标文件：{metrics_path}")
    print("")
    print(metrics.summary())

    # 指标不达标必须显式点名是哪一项 —— 只说「不通过」，运维还得自己翻 JSON。
    failed: list[str] = []
    if metrics.misbound:
        failed.append(f"错绑定 {metrics.misbound} 条（必须为 0）")
    if metrics.false_merge:
        failed.append(f"误并入 {metrics.false_merge} 条（必须为 0）")
    if metrics.dangling:
        failed.append(f"无第三去向 {metrics.dangling} 条（必须为 0）")
    if metrics.missing_decisions:
        failed.append(f"缺样本 {metrics.missing_decisions} 条（标准答案有、产品没出结论）")
    if metrics.extra_decisions:
        failed.append(f"多出样本 {metrics.extra_decisions} 条（产品出了结论、标准答案没有）")
    if metrics.duplicate_decisions:
        failed.append(f"重复决策 {metrics.duplicate_decisions} 条（同一条结果出了多个结论）")
    if metrics.reason_mismatch:
        failed.append(f"挂起原因不符 {metrics.reason_mismatch} 条")
    if metrics.ambiguous_cases and metrics.ambiguous_pending_rate < 1.0:
        failed.append(
            f"同分多候选挂起率 {metrics.ambiguous_pending_rate:.1%}"
            f"（必须 100%，{metrics.ambiguous_pending}/{metrics.ambiguous_cases}）"
        )

    if failed:
        print("", file=sys.stderr)
        print("指标不达标：", file=sys.stderr)
        for item in failed:
            print(f"  - {item}", file=sys.stderr)
        return EXIT_FAILURE

    print("")
    print("指标达标：错绑定 0、误并入 0、无第三去向 0、同分多候选挂起率 100%。")

    # ---- 三方对照：同一份输入上，本产品 vs 两种院内常见做法 -------------
    try:
        comparison = run_comparison(
            benchmark, output_dir=out_dir, database=None, run=run
        )
    except (BaselineError, Er7ParseError, OSError, ValueError) as exc:
        print(f"对照实验失败：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    print("")
    print("== 三方对照（同一份输入、同一套指标） ==")
    print(f"样本量：{comparison.sample_count} 条")
    print("")
    print(comparison.table())
    print("")
    print("口径：" + comparison.caliber)
    print("")
    print("判定：")
    print(comparison.check_lines())
    print(f"对照结果文件：{out_dir / COMPARISON_JSON_NAME}")

    if not comparison.ok:
        print("", file=sys.stderr)
        print("对照判定不通过：", file=sys.stderr)
        for check in comparison.checks:
            if not check.passed:
                print(f"  - {check.name}：{check.detail}", file=sys.stderr)
        return EXIT_FAILURE

    return EXIT_OK


def cmd_scale_report(args: argparse.Namespace) -> int:
    """规模与性能验收：跑一遍规模集，量四项指标并按阈值判定。

    与 :func:`cmd_benchmark` 的分工：基准集量的是「**对不对**」（逐条对账，
    错绑定必须为 0），这里量的是「**扛不扛得住、涨不涨得凶**」（耗时 / 对齐率 /
    内存 / 状态库体积）。规模集是脚本化合成出来的脱敏样例，没有手写标准答案，
    因此不做逐条对账。
    """
    import json

    from .config import ConfigError
    from .scale import ScaleThresholds, measure_scale

    inbox = Path(args.inbox or args.dataset).expanduser()
    work_dir = Path(args.scale_work_dir).expanduser()
    report_path = Path(args.scale_report).expanduser()

    try:
        thresholds = ScaleThresholds.from_env()
    except (ConfigError, ValueError) as exc:
        print(f"规模验收阈值配置有误：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    try:
        measurement = measure_scale(
            inbox,
            work_dir=work_dir,
            thresholds=thresholds,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"规模验收跑批失败：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(measurement.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("== 病理补充报告 · 规模与性能验收 ==")
    print(f"规模集：{measurement.inbox}")
    print(measurement.summary())
    print("")
    print("阈值判定：")
    for line in measurement.check_lines():
        print(f"  {line}")
    print("")
    print(
        "测量环境："
        f"Python {measurement.environment.get('python_version')}"
        f"｜{measurement.environment.get('platform')}"
        f"｜CPU {measurement.environment.get('cpu_count')} 核"
        f"｜测量时刻 {measurement.environment.get('measured_at')}"
    )
    print(f"样本量：{measurement.sample.get('effective_results')} 条生效结果")
    print(f"规模报告文件：{report_path}")

    if not measurement.ok:
        print("", file=sys.stderr)
        print("规模验收不达标：", file=sys.stderr)
        for check in measurement.failed_checks:
            print(f"  - {check.name}：{check.detail}", file=sys.stderr)
        return EXIT_FAILURE

    return EXIT_OK


#: 运行摘要里固定展示的五个口径（顺序即展示顺序）。
SUMMARY_CORE_ROWS: tuple[tuple[str, str], ...] = (
    ("processed", "入账结果"),
    ("aligned", "并入草稿"),
    ("pending", "挂起待人工"),
    ("writeback_ok", "写回成功"),
    ("writeback_failed", "写回失败"),
)

#: 运行摘要里附带展示的扩展口径（缺了就不显示，不占位）。
SUMMARY_EXTRA_ROWS: tuple[tuple[str, str], ...] = (
    ("elapsed_seconds", "本轮耗时（秒）"),
    ("files_scanned", "报文文件"),
    ("files_seen", "看到文件"),
    ("duplicates", "重复投递"),
    ("superseded", "更正取代"),
    ("malformed", "畸形拒收"),
    ("draft_entry_count", "草稿条目"),
    ("placeholder_count", "待回报占位"),
    ("rationale_count", "对齐依据"),
    ("derived_queries", "待查项"),
    ("recall_key_count", "检索键"),
    ("conflicts", "版本冲突"),
    ("pending_escalated", "转人工"),
    ("notify_sent", "通知发出"),
    ("notify_failed", "通知失败"),
)


def _read_json_file(path: Path) -> dict:
    """读一个 JSON 对象文件；读不出来要说清是哪个文件、为什么。"""
    import json

    if not path.is_file():
        raise FileNotFoundError(f"运行摘要不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"运行摘要读不出来（{path}）：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"运行摘要格式不对（应为 JSON 对象）：{path}")
    return data


def _normalize_run_summary(run_dir: Path) -> dict:
    """把一份运行产物目录里的摘要归一成一组口径。

    两处写摘要：批处理重放写 ``summary.json``（跑批产出的全量视图），
    守护进程写 ``round_summary.json``（与行日志同源的那五个口径）。
    两种形状字段名不同，归一后命令的读法只有一份 —— 运维不必记哪个
    命令写的哪个文件。

    归一后::

        {
          "kind": "replay" | "daemon",
          "source": 文件路径,
          "core": {processed, aligned, pending, writeback_ok, writeback_failed},
          "extras": {...},
          "drafts": [...],
          "pending_path": str | None,
          "balanced": bool | None,   # 条数守恒是否对得上（对不上时退出码非 0）
        }
    """
    daemon_path = run_dir / "round_summary.json"
    replay_path = run_dir / "summary.json"

    if daemon_path.is_file():
        data = _read_json_file(daemon_path)
        summary = data.get("summary")
        if not isinstance(summary, dict):
            raise ValueError(f"运行摘要格式不对（缺 summary 段）：{daemon_path}")
        core = {
            key: summary.get(key)
            for key, _ in SUMMARY_CORE_ROWS
        }
        return {
            "kind": "daemon",
            "source": daemon_path,
            "core": core,
            "extras": {
                key: value
                for key, value in summary.items()
                if key not in set(SUMMARY_CORE_ROWS)
            },
            "drafts": [],
            "pending_path": None,
            "balanced": None,
            "summary_line": data.get("summary_line"),
            "started_at": data.get("started_at"),
        }

    if not replay_path.is_file():
        raise FileNotFoundError(
            f"运行摘要不存在：{daemon_path} 与 {replay_path} 都没找到"
        )
    data = _read_json_file(replay_path)
    # 批处理重放不回写病理系统，写回两个口径标「不适用」，不填 0 充数 ——
    # 0 会被误读成「写了 0 次都失败了」。
    core = {
        "processed": data.get("accepted", 0),
        "aligned": data.get("rationale_count", 0),
        "pending": data.get("pending_count", 0),
        "writeback_ok": None,
        "writeback_failed": None,
    }
    accepted = int(data.get("accepted", 0) or 0)
    superseded = int(data.get("superseded", 0) or 0)
    draft_entries = int(data.get("draft_entry_count", 0) or 0)
    pending_count = int(data.get("pending_count", 0) or 0)
    return {
        "kind": "replay",
        "source": replay_path,
        "core": core,
        "extras": {
            key: data[key] for key, _ in SUMMARY_EXTRA_ROWS if key in data
        },
        "drafts": data.get("drafts") or [],
        "pending_path": data.get("pending_path"),
        "balanced": draft_entries + pending_count == accepted - superseded,
        "draft_entry_count": draft_entries,
        "pending_count": pending_count,
        "accepted": accepted,
        "superseded": superseded,
    }


def _state_db_overview(database) -> dict:
    """从状态库汇总一份运行概览。

    这一份只回答「现在库里有什麼」：草稿写到哪一步、还有多少挂起项、
    回写队列里压着几条、版本冲突有没有了结、检查项分布。
    """

    def counts(sql: str) -> dict[str, int]:
        return {
            str(row[0]): int(row[1])
            for row in database.query_all(sql)
        }

    return {
        "drafts_by_state": counts(
            "SELECT state, COUNT(*) FROM drafts GROUP BY state ORDER BY state"
        ),
        "draft_entries": int(
            database.query_value(
                "SELECT COUNT(*) FROM draft_entries WHERE placeholder = 0", default=0
            )
        ),
        "pending_by_state": counts(
            "SELECT state, COUNT(*) FROM pending_items GROUP BY state ORDER BY state"
        ),
        "writeback_queue_by_state": counts(
            "SELECT state, COUNT(*) FROM writeback_queue GROUP BY state ORDER BY state"
        ),
        "conflicts_by_state": counts(
            "SELECT state, COUNT(*) FROM writeback_conflicts GROUP BY state ORDER BY state"
        ),
        "check_items_by_state": counts(
            "SELECT state, COUNT(*) FROM check_items GROUP BY state ORDER BY state"
        ),
        "audit_by_category": counts(
            "SELECT category, COUNT(*) FROM audit_log GROUP BY category ORDER BY category"
        ),
        "audit_total": int(
            database.query_value("SELECT COUNT(*) FROM audit_log", default=0)
        ),
    }


def _print_overview_table(title: str, mapping: dict[str, int], indent: str = "  ") -> None:
    """打印一张「名称 → 条数」的表（中文按两格对齐）。"""
    if not mapping:
        print(f"{indent}{title}：（无）")
        return
    width = max(_display_width(name) for name in mapping)
    print(f"{indent}{title}：")
    for name, value in mapping.items():
        print(f"{indent}  {name}{' ' * max(1, width - _display_width(name))}{value}")


def cmd_summary(args: argparse.Namespace) -> int:
    """打印运行摘要（从运行产物目录或状态库读）。"""
    import json

    from .config import Config, ConfigError
    from .db import Database

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"配置有误，本轮不启动：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    run_dir = Path(args.run_dir).expanduser() if args.run_dir else None
    db_path = _state_db_path(args, config)

    if run_dir is None and not args.state_db and not db_path.is_file():
        # 两个来源都没给、状态库也不存在 —— 说清两条路怎么走，别只报错。
        print(
            "没给要看的来源：--dir 指向一轮的产物目录，"
            "或 --state-db 指向状态库（默认 "
            f"{db_path}）",
            file=sys.stderr,
        )
        return EXIT_USAGE

    payload: dict[str, object] = {}

    if run_dir is not None:
        try:
            data = _normalize_run_summary(run_dir)
        except (FileNotFoundError, ValueError, OSError) as exc:
            print(f"运行摘要读不出来：{exc}", file=sys.stderr)
            return EXIT_FAILURE
        payload["run"] = {
            "kind": data["kind"],
            "source": str(data["source"]),
            "core": data["core"],
            "extras": data["extras"],
            "drafts": data["drafts"],
            "pending_path": data["pending_path"],
            "balanced": data["balanced"],
        }
        if not args.as_json:
            print("== 病理补充报告 · 运行摘要 ==")
            print(f"产物目录：{run_dir}")
            print(f"摘要文件：{data['source']}")
            if data.get("started_at"):
                print(f"本轮开始：{data['started_at']}")
            print("")
            print("【本轮口径】")
            rows = []
            for key, label in SUMMARY_CORE_ROWS:
                value = data["core"].get(key)
                rows.append((label, "不适用（本轮不回写）" if value is None else str(value)))
            # 批处理重放不写回病理系统，草稿份数单独一行，不混进「写回成功」。
            if data["kind"] == "replay":
                rows.append(("草稿份数（仅落本地）", str(len(data["drafts"]))))
            print("  " + _row_text(("口径", "数值"), (22, 0)))
            for label, value in rows:
                print("  " + _row_text((label, value), (22, 0)))
            print("")
            print("【明细】")
            for key, label in SUMMARY_EXTRA_ROWS:
                if key in data["extras"]:
                    print(f"  {label:<16}{data['extras'][key]}")
            for item in data["drafts"]:
                print(
                    f"  草稿 {item.get('accession_no', '-')}"
                    f"（{item.get('entry_count', 0)} 条）→ {item.get('path', '-')}"
                )
            if data.get("pending_path"):
                print(f"  待处理清单 → {data['pending_path']}")
            if data.get("summary_line"):
                print("")
                print(f"【本轮一行摘要】{data['summary_line']}")
            # 条数守恒：入账条数应当等于「并入草稿 + 挂起 + 被取代」。
            balanced = data.get("balanced")
            if balanced is not None:
                print("")
                print(
                    f"【条数守恒】草稿 {data['draft_entry_count']} 条 + 待处理 "
                    f"{data['pending_count']} 条 = 入账 {data['accepted']} 条 "
                    f"− 更正取代 {data['superseded']} 条 "
                    f"→ {'对得上' if balanced else '对不上'}"
                )
                if not balanced:
                    print(
                        "警告：条数对不上，说明有结果既没进草稿也没进待处理清单 —— "
                        "请查审计流水。",
                        file=sys.stderr,
                    )

    if args.state_db or (run_dir is None and db_path.is_file()):
        if not db_path.is_file():
            print(f"状态库不存在：{db_path}", file=sys.stderr)
            return EXIT_FAILURE
        database = Database(db_path)
        try:
            overview = _state_db_overview(database)
        finally:
            database.close()
        payload["state_db"] = {"path": str(db_path), **overview}
        if not args.as_json:
            if run_dir is not None:
                print("")
            print("== 状态库概览 ==")
            print(f"状态库：{db_path}")
            print(f"  草稿条目 {overview['draft_entries']} 条（不含待回报占位）")
            _print_overview_table("草稿状态", overview["drafts_by_state"])
            _print_overview_table("挂起项状态", overview["pending_by_state"])
            _print_overview_table("回写队列状态", overview["writeback_queue_by_state"])
            _print_overview_table("版本冲突状态", overview["conflicts_by_state"])
            _print_overview_table("检查项状态", overview["check_items_by_state"])
            _print_overview_table("审计分类", overview["audit_by_category"])
            print(f"  审计流水合计 {overview['audit_total']} 条")

    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if payload.get("balanced") is False:
        return EXIT_FAILURE
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE
    if args.command == "pending" and not getattr(args, "pending_command", None):
        # 「pending」本身是一组子命令，不带子命令时把这一组的用法打出来。
        pending_parser = getattr(args, "pending_parser", None)
        if pending_parser is not None:
            pending_parser.print_help()
        else:  # pragma: no cover - 解析器结构变了才走这里
            parser.print_help()
        return EXIT_USAGE
    if args.command == "audit" and not getattr(args, "audit_command", None):
        audit_parser = getattr(args, "audit_parser", None)
        if audit_parser is not None:
            audit_parser.print_help()
        else:  # pragma: no cover - 解析器结构变了才走这里
            parser.print_help()
        return EXIT_USAGE
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
