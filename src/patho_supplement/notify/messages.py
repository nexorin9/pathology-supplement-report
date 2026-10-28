"""通知正文渲染。

两种通知，两种人看：

- **草稿待签发**：成稿已经写回病理系统草稿区了，值班的人需要知道「哪份报告
  可以签了、签完还剩几项没回来」。
- **升级告警**：草稿写不回去了（重试用尽、版本冲突），需要有人去处理。

正文只写业务标识（病理号、版本号、条目数），不写结果值、不写患者信息 ——
通知是要往外发的件（邮箱、群机器人），带出去的每一个字都要经得起问。
正文出门前再过一遍脱敏，防止上游文本里混进了姓名或电话。
"""

from __future__ import annotations

from ..privacy import desensitize_text
from .base import KIND_DRAFT, KIND_ESCALATION, NotifyMessage

__all__ = [
    "build_draft_notice",
    "build_escalation_notice",
]


def build_draft_notice(
    *,
    accession_no: str,
    version: int,
    entry_count: int,
    pending_count: int = 0,
    accepted_version: str | None = None,
    recipients: tuple[str, ...] = (),
) -> NotifyMessage:
    """「补充报告草稿已回写，等待签发」的通知。

    参数里的 ``entry_count`` 是并入草稿的结果条数，``pending_count`` 是还要
    人补齐的条数 —— 两者都写上，值班的人一眼知道这份报告是不是「齐活」的。
    """
    subject = f"补充报告草稿待签发｜病理号 {accession_no}"
    version_text = f"v{version}" if version else "（版本号未知）"
    body_lines = [
        f"病理号：{accession_no}",
        f"补充报告草稿 {version_text} 已回写到病理系统草稿区，等待签发。",
        f"本次并入结果 {entry_count} 条，待人工处理 {pending_count} 条。",
    ]
    if accepted_version:
        body_lines.append(f"病理系统接受版本：{accepted_version}")
    body_lines.extend(
        [
            "",
            "请在病理系统中打开该报告的补充报告草稿并签发。",
        ]
    )
    body = "\n".join(body_lines)
    markdown_lines = [
        f"**病理号：{accession_no}**",
        f"补充报告草稿 {version_text} 已回写，等待签发。",
        f"> 并入结果 {entry_count} 条，待人工处理 {pending_count} 条",
    ]
    if accepted_version:
        markdown_lines.append(f"> 病理系统接受版本：{accepted_version}")
    markdown_lines.append("请在病理系统中签发该草稿。")
    markdown = "\n".join(markdown_lines)
    return NotifyMessage(
        subject=subject,
        body=desensitize_text(body),
        markdown=desensitize_text(markdown),
        recipients=tuple(recipients),
        accession_no=accession_no,
        kind=KIND_DRAFT,
        facts={
            "draft_version": version,
            "entry_count": entry_count,
            "pending_count": pending_count,
            "accepted_version": accepted_version,
        },
    )


def build_escalation_notice(
    *,
    accession_no: str,
    draft_version: int | None,
    attempts: int,
    max_retries: int,
    last_error: str,
    action: str,
    recipients: tuple[str, ...] = (),
) -> NotifyMessage:
    """「这份草稿写不回去了，请人工处置」的告警通知。

    告警必须说清三件事：哪份草稿、试了几次、最后错在哪、下一步谁做什么。
    少了任何一条，值班的人还得自己去翻状态库 —— 那就等于没通知。
    """
    version_text = f"v{draft_version}" if draft_version else "（版本号未知）"
    subject = f"补充报告草稿回写失败｜病理号 {accession_no}"
    body_lines = [
        f"病理号：{accession_no}",
        f"补充报告草稿 {version_text} 回写病理系统失败，已重试 {attempts} 次"
        f"（上限 {max_retries} 次）仍未成功。",
        f"最后一次错误：{last_error}",
        f"处理建议：{action}",
    ]
    body = "\n".join(body_lines)
    markdown_lines = [
        f"**病理号：{accession_no}**",
        f"补充报告草稿 {version_text} 回写失败，已重试 {attempts} 次仍未成功。",
        f"> 最后一次错误：{last_error}",
        f"> 处理建议：{action}",
    ]
    markdown = "\n".join(markdown_lines)
    return NotifyMessage(
        subject=subject,
        body=desensitize_text(body),
        markdown=desensitize_text(markdown),
        recipients=tuple(recipients),
        accession_no=accession_no,
        kind=KIND_ESCALATION,
        facts={
            "draft_version": draft_version,
            "attempts": attempts,
            "max_retries": max_retries,
        },
    )
