"""环境变量配置面。

院内机上的对接地址与凭据全部来自环境变量或 ``.env`` 文件；
本模块里不出现任何真实地址、端口或密钥，也不提供带真实值的默认项。

设计要点：
- 必填项缺失时给出**可读**的缺项清单（信息科照着清单补环境变量即可）；
- 数值项缺省用内置默认值，越界直接拒绝启动并说明原因，不静默改用默认值；
- 路径统一规范化（展开 ``~``、转绝对路径），避免各模块各自拼路径。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import ClassVar, Mapping

__all__ = [
    "Config",
    "ConfigError",
    "load_dotenv",
    "REQUIRED_ENV_KEYS",
    "SECRET_ENV_KEYS",
    "DEFAULT_BM25_THRESHOLD",
    "DEFAULT_LEAD_MARGIN_THRESHOLD",
    "DEFAULT_FILTER_POLICY",
    "DEFAULT_FANOUT_MAX_WORKERS",
    "DEFAULT_PENDING_MAX_ATTEMPTS",
    "DEFAULT_PENDING_LOCK_TTL",
    "DEFAULT_WRITEBACK_RETRY_BASE_SECONDS",
    "DEFAULT_WRITEBACK_RETRY_MAX_SECONDS",
    "DEFAULT_NOTIFY_MAX_ATTEMPTS",
    "DEFAULT_NOTIFY_RETRY_DELAY_SECONDS",
    "DEFAULT_SCALE_MAX_ELAPSED_SECONDS",
    "DEFAULT_SCALE_MIN_ALIGNMENT_RATE",
    "DEFAULT_SCALE_MAX_PENDING_RATE",
    "DEFAULT_SCALE_MAX_PEAK_MEMORY_MB",
    "DEFAULT_SCALE_MAX_STATE_DB_MB",
    "DEFAULT_SMTP_HELO_NAME",
    "DEFAULT_DUTY_DEPARTMENT",
    "DEFAULT_LOG_LEVEL",
]

#: 判定阈值与运行参数的缺省值（缺项时用这些值，不静默改用别的）。
DEFAULT_BM25_THRESHOLD = 0.32
DEFAULT_LEAD_MARGIN_THRESHOLD = 0.05
DEFAULT_FILTER_POLICY = "MERGE"
DEFAULT_FANOUT_MAX_WORKERS = 4
DEFAULT_FANOUT_ROUTE_TIMEOUT = 5.0
#: 挂起项尝试次数上限：同一条结果反复对不上，超过上限就转人工处置，不再自动续跑。
DEFAULT_PENDING_MAX_ATTEMPTS = 3
#: 挂起行互斥锁的有效期（秒）：持锁的进程崩了，锁不能永远挂着。
DEFAULT_PENDING_LOCK_TTL = 300.0
#: 回写重试的退避基数（秒）：第 1 次写回失败后等这么久再试。
DEFAULT_WRITEBACK_RETRY_BASE_SECONDS = 5.0
#: 回写重试的退避封顶（秒）：再往上等也没意义，院内机不能把重试拖到隔天。
DEFAULT_WRITEBACK_RETRY_MAX_SECONDS = 900.0
#: 单条通知通道的默认重试次数（首发 + 重试 1 次）。
DEFAULT_NOTIFY_MAX_ATTEMPTS = 2
#: 通知重试的间隔基数（秒）：第 1 次重试等这么久，第 2 次等两倍。
DEFAULT_NOTIFY_RETRY_DELAY_SECONDS = 1.0
#: SMTP 会话里报的机器名。写死一个名字，发信就不等本机域名反查 ——
#: 院内机的 DNS 常常配不全，反查一次能卡住几十秒。
DEFAULT_SMTP_HELO_NAME = "localhost"
#: 科室名（缺省）。排班表若声明了别的科室，装载时会留告警 ——
#: 拿别的科室的排班表发通知，等于把报告发给了不相干的人。
DEFAULT_DUTY_DEPARTMENT = "病理科"
#: 日志级别（缺省）。行日志按事件一行记，DEBUG 会把每一步都铺开。
DEFAULT_LOG_LEVEL = "INFO"
#: 定时兜底扫描的间隔（秒，缺省）：结果目录长时间没新文件时，按这个周期
#: 强制扫一遍 —— 目录监听靠「文件落稳」判定，机器重启后那一轮判定要重来，
#: 兜底扫描保证停机期间投进来的文件不会没人管。
DEFAULT_WATCH_FALLBACK_INTERVAL = 60.0
#: 规模验收的阈值（缺省值）。实测 2200 份报文约 1.5 秒；院内机再慢十倍也
#: 留得下余量，但慢到分钟级就说明某处退化了。
DEFAULT_SCALE_MAX_ELAPSED_SECONDS = 60.0
#: 单轮对齐率下限：并入草稿的条目占比不得低于这个数 —— 低于它说明大批结果
#: 进了挂起清单，等于白跑一轮。
DEFAULT_SCALE_MIN_ALIGNMENT_RATE = 0.60
#: 挂起率上限（分母同为生效结果数，与对齐率互补）。留一点余量：无法获得 /
#: 值为空 / 回报时间倒挂这几类本来就该扣下等人。
DEFAULT_SCALE_MAX_PENDING_RATE = 0.40
#: 峰值内存增量上限（MB）。状态只落 SQLite，中间对象一轮结束就释放。
DEFAULT_SCALE_MAX_PEAK_MEMORY_MB = 256.0
#: 状态库体积上限（MB）。实测约 2.9 MB；院内机磁盘不宽裕，别让它无界涨。
DEFAULT_SCALE_MAX_STATE_DB_MB = 64.0

#: 启动出站与入站功能所必需的环境变量键名。
REQUIRED_ENV_KEYS: tuple[str, ...] = (
    "PATHOLOGY_BASE_URL",
    "PATHOLOGY_API_TOKEN",
    "PATHOLOGY_RESULT_INBOX",
)

#: 属于凭据的键名：自检输出时只报「是否已配置」，绝不回显值本身。
SECRET_ENV_KEYS: tuple[str, ...] = (
    "PATHOLOGY_API_TOKEN",
    "SMTP_PASSWORD",
    "SMTP_USER",
    "WECOM_WEBHOOK_URL",
)

_ENV_FILE_NAME = ".env"


class ConfigError(RuntimeError):
    """配置缺失或越界。

    消息面向运维，必须一眼看出「缺什么」或「哪一项填错了、合法范围是什么」。
    """


def load_dotenv(path: Path | str | None = None) -> dict[str, str]:
    """读取 ``.env`` 文件并注入环境变量。

    已存在的环境变量优先，``.env`` 不覆盖 —— 这样在院内机上用 systemd 注入的
    凭据不会被一个误提交的 ``.env`` 顶掉。

    返回本次从文件读到的键值对（便于 ``doctor`` 打印来源）。
    """
    env_path = Path(path) if path is not None else Path(_ENV_FILE_NAME)
    if not env_path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value

    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def _text(env: Mapping[str, str], key: str) -> str | None:
    raw = env.get(key)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _path(env: Mapping[str, str], key: str, default: str | None = None) -> Path | None:
    raw = _text(env, key) or default
    if raw is None:
        return None
    return Path(raw).expanduser().resolve()


def _int(env: Mapping[str, str], key: str, default: int, *, minimum: int | None = None,
         maximum: int | None = None) -> int:
    raw = _text(env, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {key} 必须是整数，当前值 {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"环境变量 {key} 不得小于 {minimum}，当前值 {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"环境变量 {key} 不得大于 {maximum}，当前值 {value}")
    return value


def _float(env: Mapping[str, str], key: str, default: float, *, minimum: float | None = None,
           maximum: float | None = None) -> float:
    raw = _text(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {key} 必须是数值，当前值 {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"环境变量 {key} 不得小于 {minimum}，当前值 {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"环境变量 {key} 不得大于 {maximum}，当前值 {value}")
    return value


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    """布尔型环境变量。

    只认几个常见写法（``true/false``、``1/0``、``yes/no``、``on/off``）；
    认不出来就报错 —— 宁可启动失败，也不要把 ``SMTP_STARTTLS=false`` 当成
    ``true`` 悄悄发出去。
    """
    raw = _text(env, key)
    if raw is None:
        return default
    value = raw.lower()
    if value in ("1", "true", "yes", "on", "是", "开"):
        return True
    if value in ("0", "false", "no", "off", "否", "关"):
        return False
    raise ConfigError(
        f"环境变量 {key} 只能是 true/false（或 1/0、yes/no、on/off），当前值 {raw!r}"
    )


def _recipients(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    """收件人列表（逗号 / 中文逗号 / 分号分隔）。"""
    raw = _text(env, key)
    if raw is None:
        return ()
    parts: list[str] = []
    for chunk in raw.replace("，", ",").replace("；", ",").replace(";", ",").split(","):
        value = chunk.strip()
        if value:
            parts.append(value)
    return tuple(parts)


@dataclass(frozen=True)
class Config:
    """一次运行用到的全部配置。

    字段全部有安全默认值或明确的缺失态（``None``），不会出现「悄悄用了别处的值」。
    """

    # 病理信息系统出站
    pathology_base_url: str | None = None
    pathology_api_token: str | None = None
    # 入站
    pathology_result_inbox: Path | None = None
    pathology_state_db: Path | None = None
    # 部位同义词典（外置 JSON；缺省用内置最小词表）
    lexicon_path: Path | None = None
    # 通知
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    #: 隐式 TLS（465 端口常用）；与 STARTTLS 二选一。
    smtp_use_ssl: bool = False
    #: STARTTLS（587 端口常用）：连上后再升级为加密连接。
    smtp_starttls: bool = True
    #: EHLO/HELO 里报的机器名（写死一个名字，避免发信时等 DNS 反查）。
    smtp_helo_name: str = DEFAULT_SMTP_HELO_NAME
    wecom_webhook_url: str | None = None
    #: 默认收件人（逗号分隔）；排班表命中时按排班走，这里是兜底。
    notify_recipients: tuple[str, ...] = ()
    #: 当班排班表文件（外置 JSON）；命中就按当班医师发，没命中降级到默认收件人。
    notify_duty_roster_path: Path | None = None
    #: 科室名（排班表若声明了别的科室，装载时会留告警）。
    notify_duty_department: str = DEFAULT_DUTY_DEPARTMENT
    #: 单条通道的通知重试次数。
    notify_max_attempts: int = DEFAULT_NOTIFY_MAX_ATTEMPTS
    #: 通知重试的间隔基数（秒）。
    notify_retry_delay_seconds: float = DEFAULT_NOTIFY_RETRY_DELAY_SECONDS
    #: 通知通道的超时秒数。
    notify_timeout: float = 10.0
    # 日志
    #: 结构化行日志的级别（DEBUG / INFO / WARNING / ERROR）。
    log_level: str = DEFAULT_LOG_LEVEL
    #: 行日志落盘文件；不给就写标准错误。
    log_file: Path | None = None
    #: 是否把**结果值**原样写进日志。默认关 —— 日志常要发给信息科看。
    log_result_values: bool = False
    # 判定阈值
    bm25_threshold: float = DEFAULT_BM25_THRESHOLD
    lead_margin_threshold: float = DEFAULT_LEAD_MARGIN_THRESHOLD
    filter_policy: str = DEFAULT_FILTER_POLICY
    fanout_max_workers: int = DEFAULT_FANOUT_MAX_WORKERS
    fanout_route_timeout: float = DEFAULT_FANOUT_ROUTE_TIMEOUT
    # 挂起项尝试次数上限（超过即转人工处置，不再自动续跑）
    pending_max_attempts: int = DEFAULT_PENDING_MAX_ATTEMPTS
    # 挂起行互斥锁的有效期（秒）：超过即视为过期锁，可被接管
    pending_lock_ttl: float = DEFAULT_PENDING_LOCK_TTL
    # 运行参数
    watch_poll_interval: float = 2.0
    watch_settle_seconds: float = 0.5
    #: 定时兜底扫描的间隔（秒）：结果目录长时间没动静时，按这个周期强制扫一遍，
    #: 接住守护进程停机期间投进来的文件。
    watch_fallback_interval: float = DEFAULT_WATCH_FALLBACK_INTERVAL
    #: 单实例锁文件路径；不给就放在状态库同级（``<状态库目录>/patho_supplement.lock``）。
    daemon_lock_path: Path | None = None
    writeback_max_retries: int = 5
    #: 回写重试的退避基数（秒）：第 1 次写回失败后等这么久再试。
    writeback_retry_base_seconds: float = DEFAULT_WRITEBACK_RETRY_BASE_SECONDS
    #: 回写重试的退避封顶（秒）。
    writeback_retry_max_seconds: float = DEFAULT_WRITEBACK_RETRY_MAX_SECONDS
    http_timeout: float = 10.0
    # 规模与性能验收阈值（超阈即判定不合格）
    #: 端到端耗时上限（秒）。
    scale_max_elapsed_seconds: float = DEFAULT_SCALE_MAX_ELAPSED_SECONDS
    #: 单轮对齐率下限。
    scale_min_alignment_rate: float = DEFAULT_SCALE_MIN_ALIGNMENT_RATE
    #: 挂起率上限（与对齐率互补）。
    scale_max_pending_rate: float = DEFAULT_SCALE_MAX_PENDING_RATE
    #: 峰值内存增量上限（MB）。
    scale_max_peak_memory_mb: float = DEFAULT_SCALE_MAX_PEAK_MEMORY_MB
    #: 状态库体积上限（MB）。
    scale_max_state_db_mb: float = DEFAULT_SCALE_MAX_STATE_DB_MB

    #: 允许的硬过滤策略取值。
    FILTER_POLICIES: ClassVar[tuple[str, ...]] = ("REPLACE", "MERGE")

    # ---------------------------------------------------------------- 构造

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv_path: Path | str | None = None,
        *,
        strict: bool = False,
    ) -> "Config":
        """从环境变量构造配置。

        ``strict=True`` 时缺任何必填项即抛 :class:`ConfigError`；
        默认 ``strict=False``，缺失项留 ``None``，由 ``missing_required()`` 与
        ``doctor`` 子命令负责报缺项清单。
        """
        source = os.environ if env is None else env
        if env is None:
            load_dotenv(dotenv_path)

        smtp_port_raw = _text(source, "SMTP_PORT")
        smtp_port: int | None = None
        if smtp_port_raw is not None:
            try:
                smtp_port = int(smtp_port_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"环境变量 SMTP_PORT 必须是整数，当前值 {smtp_port_raw!r}"
                ) from exc
            if not 1 <= smtp_port <= 65535:
                raise ConfigError(
                    f"环境变量 SMTP_PORT 必须在 1-65535 之间，当前值 {smtp_port}"
                )

        policy = (_text(source, "FILTER_POLICY") or DEFAULT_FILTER_POLICY).upper()
        if policy not in ("REPLACE", "MERGE"):
            raise ConfigError(
                "环境变量 FILTER_POLICY 只能是 REPLACE 或 MERGE，当前值 " f"{policy!r}"
            )

        # 日志级别：认不出来的写法直接报错 —— 「日志没出来」比「日志级别配错
        # 却悄悄按默认跑」难查得多。
        log_level = (_text(source, "LOG_LEVEL") or DEFAULT_LOG_LEVEL).upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError(
                "环境变量 LOG_LEVEL 只能是 DEBUG、INFO、WARNING 或 ERROR，当前值 "
                f"{log_level!r}"
            )

        config = cls(
            pathology_base_url=_text(source, "PATHOLOGY_BASE_URL"),
            pathology_api_token=_text(source, "PATHOLOGY_API_TOKEN"),
            pathology_result_inbox=_path(source, "PATHOLOGY_RESULT_INBOX"),
            pathology_state_db=_path(source, "PATHOLOGY_STATE_DB", "data/patho_state.db"),
            lexicon_path=_path(source, "PATHOLOGY_LEXICON_PATH"),
            smtp_host=_text(source, "SMTP_HOST"),
            smtp_port=smtp_port,
            smtp_user=_text(source, "SMTP_USER"),
            smtp_password=_text(source, "SMTP_PASSWORD"),
            smtp_from=_text(source, "SMTP_FROM"),
            smtp_use_ssl=_bool(source, "SMTP_USE_SSL", False),
            smtp_starttls=_bool(source, "SMTP_STARTTLS", True),
            smtp_helo_name=_text(source, "SMTP_HELO_NAME") or DEFAULT_SMTP_HELO_NAME,
            wecom_webhook_url=_text(source, "WECOM_WEBHOOK_URL"),
            notify_recipients=_recipients(source, "NOTIFY_RECIPIENTS"),
            notify_duty_roster_path=_path(source, "NOTIFY_DUTY_ROSTER_PATH"),
            notify_duty_department=(
                _text(source, "NOTIFY_DUTY_DEPARTMENT") or DEFAULT_DUTY_DEPARTMENT
            ),
            notify_max_attempts=_int(
                source,
                "NOTIFY_MAX_ATTEMPTS",
                DEFAULT_NOTIFY_MAX_ATTEMPTS,
                minimum=1,
                maximum=10,
            ),
            notify_retry_delay_seconds=_float(
                source,
                "NOTIFY_RETRY_DELAY_SECONDS",
                DEFAULT_NOTIFY_RETRY_DELAY_SECONDS,
                minimum=0.0,
                maximum=60.0,
            ),
            notify_timeout=_float(
                source, "NOTIFY_TIMEOUT", 10.0, minimum=0.1, maximum=600.0
            ),
            log_level=log_level,
            log_file=_path(source, "LOG_FILE"),
            log_result_values=_bool(source, "LOG_RESULT_VALUES", False),
            bm25_threshold=_float(
                source, "BM25_THRESHOLD", DEFAULT_BM25_THRESHOLD, minimum=0.0, maximum=1.0
            ),
            lead_margin_threshold=_float(
                source,
                "LEAD_MARGIN_THRESHOLD",
                DEFAULT_LEAD_MARGIN_THRESHOLD,
                minimum=0.0,
                maximum=1.0,
            ),
            filter_policy=policy,
            fanout_max_workers=_int(
                source, "FANOUT_MAX_WORKERS", DEFAULT_FANOUT_MAX_WORKERS, minimum=1, maximum=32
            ),
            fanout_route_timeout=_float(
                source,
                "FANOUT_ROUTE_TIMEOUT",
                DEFAULT_FANOUT_ROUTE_TIMEOUT,
                minimum=0.1,
                maximum=600.0,
            ),
            pending_max_attempts=_int(
                source,
                "PENDING_MAX_ATTEMPTS",
                DEFAULT_PENDING_MAX_ATTEMPTS,
                minimum=1,
                maximum=50,
            ),
            pending_lock_ttl=_float(
                source,
                "PENDING_LOCK_TTL",
                DEFAULT_PENDING_LOCK_TTL,
                minimum=1.0,
                maximum=3600.0,
            ),
            watch_poll_interval=_float(
                source, "WATCH_POLL_INTERVAL", 2.0, minimum=0.1, maximum=3600.0
            ),
            watch_settle_seconds=_float(
                source, "WATCH_SETTLE_SECONDS", 0.5, minimum=0.0, maximum=600.0
            ),
            watch_fallback_interval=_float(
                source,
                "WATCH_FALLBACK_INTERVAL",
                DEFAULT_WATCH_FALLBACK_INTERVAL,
                minimum=1.0,
                maximum=86400.0,
            ),
            daemon_lock_path=_path(source, "PATHOLOGY_LOCK_FILE"),
            writeback_max_retries=_int(
                source, "WRITEBACK_MAX_RETRIES", 5, minimum=0, maximum=50
            ),
            writeback_retry_base_seconds=_float(
                source,
                "WRITEBACK_RETRY_BASE_SECONDS",
                DEFAULT_WRITEBACK_RETRY_BASE_SECONDS,
                minimum=0.1,
                maximum=3600.0,
            ),
            writeback_retry_max_seconds=_float(
                source,
                "WRITEBACK_RETRY_MAX_SECONDS",
                DEFAULT_WRITEBACK_RETRY_MAX_SECONDS,
                minimum=1.0,
                maximum=86400.0,
            ),
            http_timeout=_float(source, "HTTP_TIMEOUT", 10.0, minimum=0.1, maximum=600.0),
            scale_max_elapsed_seconds=_float(
                source,
                "SCALE_MAX_ELAPSED_SECONDS",
                DEFAULT_SCALE_MAX_ELAPSED_SECONDS,
                minimum=0.1,
                maximum=86400.0,
            ),
            scale_min_alignment_rate=_float(
                source,
                "SCALE_MIN_ALIGNMENT_RATE",
                DEFAULT_SCALE_MIN_ALIGNMENT_RATE,
                minimum=0.0,
                maximum=1.0,
            ),
            scale_max_pending_rate=_float(
                source,
                "SCALE_MAX_PENDING_RATE",
                DEFAULT_SCALE_MAX_PENDING_RATE,
                minimum=0.0,
                maximum=1.0,
            ),
            scale_max_peak_memory_mb=_float(
                source,
                "SCALE_MAX_PEAK_MEMORY_MB",
                DEFAULT_SCALE_MAX_PEAK_MEMORY_MB,
                minimum=1.0,
                maximum=1048576.0,
            ),
            scale_max_state_db_mb=_float(
                source,
                "SCALE_MAX_STATE_DB_MB",
                DEFAULT_SCALE_MAX_STATE_DB_MB,
                minimum=0.1,
                maximum=1048576.0,
            ),
        )

        if strict:
            missing = config.missing_required()
            if missing:
                raise ConfigError(
                    "缺少必需的环境变量：" + "、".join(missing) + "（参考 .env.example 填写）"
                )
        return config

    # ---------------------------------------------------------------- 检查

    def missing_required(self) -> list[str]:
        """返回缺失的必填环境变量键名（按固定顺序，便于逐条补齐）。"""
        present = {
            "PATHOLOGY_BASE_URL": self.pathology_base_url,
            "PATHOLOGY_API_TOKEN": self.pathology_api_token,
            "PATHOLOGY_RESULT_INBOX": self.pathology_result_inbox,
        }
        return [key for key in REQUIRED_ENV_KEYS if not present.get(key)]

    def credential_status(self) -> dict[str, bool]:
        """各凭据位「是否已配置」；只回显状态，不回显值。"""
        return {
            "PATHOLOGY_BASE_URL": bool(self.pathology_base_url),
            "PATHOLOGY_API_TOKEN": bool(self.pathology_api_token),
            "PATHOLOGY_RESULT_INBOX": bool(self.pathology_result_inbox),
            "SMTP_HOST": bool(self.smtp_host),
            "SMTP_USER": bool(self.smtp_user),
            "SMTP_PASSWORD": bool(self.smtp_password),
            "SMTP_FROM": bool(self.smtp_from),
            "NOTIFY_RECIPIENTS": bool(self.notify_recipients),
            "WECOM_WEBHOOK_URL": bool(self.wecom_webhook_url),
        }

    def effective_thresholds(self) -> dict[str, object]:
        """当前生效的判定阈值与运行参数，供 ``doctor`` 打印。"""
        return {
            "bm25_threshold": self.bm25_threshold,
            "lead_margin_threshold": self.lead_margin_threshold,
            "filter_policy": self.filter_policy,
            "fanout_max_workers": self.fanout_max_workers,
            "fanout_route_timeout": self.fanout_route_timeout,
            "pending_max_attempts": self.pending_max_attempts,
            "pending_lock_ttl": self.pending_lock_ttl,
            "writeback_max_retries": self.writeback_max_retries,
            "writeback_retry_base_seconds": self.writeback_retry_base_seconds,
            "writeback_retry_max_seconds": self.writeback_retry_max_seconds,
            "http_timeout": self.http_timeout,
            "watch_poll_interval": self.watch_poll_interval,
            "watch_settle_seconds": self.watch_settle_seconds,
            "watch_fallback_interval": self.watch_fallback_interval,
            "daemon_lock_path": str(self.daemon_lock_path) if self.daemon_lock_path else "",
            "notify_max_attempts": self.notify_max_attempts,
            "notify_retry_delay_seconds": self.notify_retry_delay_seconds,
            "notify_timeout": self.notify_timeout,
            "log_level": self.log_level,
            "log_file": str(self.log_file) if self.log_file else "",
            "log_result_values": self.log_result_values,
            "scale_max_elapsed_seconds": self.scale_max_elapsed_seconds,
            "scale_min_alignment_rate": self.scale_min_alignment_rate,
            "scale_max_pending_rate": self.scale_max_pending_rate,
            "scale_max_peak_memory_mb": self.scale_max_peak_memory_mb,
            "scale_max_state_db_mb": self.scale_max_state_db_mb,
        }

    def threshold_rows(self) -> list[dict[str, object]]:
        """可打印的阈值清单（``doctor`` 用）。

        每行给出中文名、环境变量键、当前生效值、缺省值与合法范围 ——
        运维照着这一张表就能判断「配错了哪一项、该改成什么」。
        """
        return [
            {
                "name": "词法命中阈值",
                "env": "BM25_THRESHOLD",
                "value": self.bm25_threshold,
                "default": DEFAULT_BM25_THRESHOLD,
                "spec": "0–1 之间的数值",
            },
            {
                "name": "领先幅度阈值",
                "env": "LEAD_MARGIN_THRESHOLD",
                "value": self.lead_margin_threshold,
                "default": DEFAULT_LEAD_MARGIN_THRESHOLD,
                "spec": "0–1 之间的数值",
            },
            {
                "name": "硬过滤策略",
                "env": "FILTER_POLICY",
                "value": self.filter_policy,
                "default": DEFAULT_FILTER_POLICY,
                "spec": "REPLACE 或 MERGE",
            },
            {
                "name": "召回并发数",
                "env": "FANOUT_MAX_WORKERS",
                "value": self.fanout_max_workers,
                "default": DEFAULT_FANOUT_MAX_WORKERS,
                "spec": "1–32 的整数",
            },
            {
                "name": "单路召回超时",
                "env": "FANOUT_ROUTE_TIMEOUT",
                "value": self.fanout_route_timeout,
                "default": DEFAULT_FANOUT_ROUTE_TIMEOUT,
                "spec": "0.1–600 秒",
            },
            {
                "name": "挂起尝试上限",
                "env": "PENDING_MAX_ATTEMPTS",
                "value": self.pending_max_attempts,
                "default": DEFAULT_PENDING_MAX_ATTEMPTS,
                "spec": "1–50 的整数",
            },
            {
                "name": "挂起行锁有效期",
                "env": "PENDING_LOCK_TTL",
                "value": self.pending_lock_ttl,
                "default": DEFAULT_PENDING_LOCK_TTL,
                "spec": "1–3600 秒",
            },
            {
                "name": "回写重试上限",
                "env": "WRITEBACK_MAX_RETRIES",
                "value": self.writeback_max_retries,
                "default": 5,
                "spec": "0–50 的整数",
            },
            {
                "name": "回写退避基数",
                "env": "WRITEBACK_RETRY_BASE_SECONDS",
                "value": self.writeback_retry_base_seconds,
                "default": DEFAULT_WRITEBACK_RETRY_BASE_SECONDS,
                "spec": "0.1–3600 秒",
            },
            {
                "name": "回写退避封顶",
                "env": "WRITEBACK_RETRY_MAX_SECONDS",
                "value": self.writeback_retry_max_seconds,
                "default": DEFAULT_WRITEBACK_RETRY_MAX_SECONDS,
                "spec": "1–86400 秒",
            },
            {
                "name": "出站超时",
                "env": "HTTP_TIMEOUT",
                "value": self.http_timeout,
                "default": 10.0,
                "spec": "0.1–600 秒",
            },
            {
                "name": "目录轮询间隔",
                "env": "WATCH_POLL_INTERVAL",
                "value": self.watch_poll_interval,
                "default": 2.0,
                "spec": "0.1–3600 秒",
            },
            {
                "name": "落稳等待",
                "env": "WATCH_SETTLE_SECONDS",
                "value": self.watch_settle_seconds,
                "default": 0.5,
                "spec": "0–600 秒",
            },
            {
                "name": "兜底扫描间隔",
                "env": "WATCH_FALLBACK_INTERVAL",
                "value": self.watch_fallback_interval,
                "default": DEFAULT_WATCH_FALLBACK_INTERVAL,
                "spec": "1–86400 秒",
            },
            {
                "name": "单实例锁文件",
                "env": "PATHOLOGY_LOCK_FILE",
                "value": str(self.daemon_lock_path) if self.daemon_lock_path else "（未配置）",
                "default": "（未配置）",
                "spec": "可写路径；不配置则放在状态库同级",
            },
            {
                "name": "通知重试次数",
                "env": "NOTIFY_MAX_ATTEMPTS",
                "value": self.notify_max_attempts,
                "default": DEFAULT_NOTIFY_MAX_ATTEMPTS,
                "spec": "1–10 的整数",
            },
            {
                "name": "通知重试间隔",
                "env": "NOTIFY_RETRY_DELAY_SECONDS",
                "value": self.notify_retry_delay_seconds,
                "default": DEFAULT_NOTIFY_RETRY_DELAY_SECONDS,
                "spec": "0–60 秒",
            },
            {
                "name": "通知超时",
                "env": "NOTIFY_TIMEOUT",
                "value": self.notify_timeout,
                "default": 10.0,
                "spec": "0.1–600 秒",
            },
            {
                "name": "日志级别",
                "env": "LOG_LEVEL",
                "value": self.log_level,
                "default": DEFAULT_LOG_LEVEL,
                "spec": "DEBUG / INFO / WARNING / ERROR",
            },
            {
                "name": "日志文件",
                "env": "LOG_FILE",
                "value": str(self.log_file) if self.log_file else "（未配置，写标准错误）",
                "default": "（未配置）",
                "spec": "可写路径；不配置则输出到标准错误",
            },
            {
                "name": "结果值入日志",
                "env": "LOG_RESULT_VALUES",
                "value": "是" if self.log_result_values else "否",
                "default": "否",
                "spec": "true / false；默认不把判读结果写进日志",
            },
            {
                "name": "规模验收耗时上限",
                "env": "SCALE_MAX_ELAPSED_SECONDS",
                "value": self.scale_max_elapsed_seconds,
                "default": DEFAULT_SCALE_MAX_ELAPSED_SECONDS,
                "spec": "0.1–86400 秒",
            },
            {
                "name": "规模验收对齐率下限",
                "env": "SCALE_MIN_ALIGNMENT_RATE",
                "value": self.scale_min_alignment_rate,
                "default": DEFAULT_SCALE_MIN_ALIGNMENT_RATE,
                "spec": "0–1 之间的数值",
            },
            {
                "name": "规模验收挂起率上限",
                "env": "SCALE_MAX_PENDING_RATE",
                "value": self.scale_max_pending_rate,
                "default": DEFAULT_SCALE_MAX_PENDING_RATE,
                "spec": "0–1 之间的数值（与对齐率互补）",
            },
            {
                "name": "规模验收内存上限",
                "env": "SCALE_MAX_PEAK_MEMORY_MB",
                "value": self.scale_max_peak_memory_mb,
                "default": DEFAULT_SCALE_MAX_PEAK_MEMORY_MB,
                "spec": "1–1048576 MB",
            },
            {
                "name": "规模验收库体积上限",
                "env": "SCALE_MAX_STATE_DB_MB",
                "value": self.scale_max_state_db_mb,
                "default": DEFAULT_SCALE_MAX_STATE_DB_MB,
                "spec": "0.1–1048576 MB",
            },
        ]

    def as_dict(self) -> dict[str, object]:
        """可打印的配置视图（凭据位只给布尔状态）。"""
        data: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            data[f.name] = str(value) if isinstance(value, Path) else value
        data["credential_status"] = self.credential_status()
        return data
