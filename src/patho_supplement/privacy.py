"""脱敏原语：把可识别信息从要外发的文本里遮掉。

清单、导出、通知都要往外走 —— 打印出来、发给技术员、塞进病案柜。带出去的件
不该再带出患者身份，所以自由文本在出门前一律过一遍这里。

两条口径：

- **形态明确的标识符**（身份证号、手机号、固定电话、邮箱、银行卡号）：直接遮蔽，
  换成占位符 —— 保留「这里原本有东西」的事实，技术员才知道要回去问什么。
- **姓名**：形态不明确 —— 中文姓名和「宫颈」「乳腺」这类部位词长得像，认错了
  就把部位描述抹了。所以只用两种认法：**带提示词的**（患者X、姓名X）与**高频
  姓氏起头**的裸姓名；带职称的（王主任）不算姓名。

认不出来就不遮 —— 宁可漏遮一个可疑串，也不把业务字段抹掉。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = [
    "SENSITIVE_FIELD_NAMES",
    "desensitize_fields",
    "desensitize_text",
    "is_sensitive_field",
]

# ---------------------------------------------------------------- 字段级

#: 患者可识别字段名（英文小写与中文都收）。这些字段**整个不进导出** ——
#: 清单不需要它们，也不该带它们走。
SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "patientname",
        "patient",
        "name",
        "姓名",
        "患者姓名",
        "病人姓名",
        "受检者姓名",
        "idcard",
        "idcardno",
        "身份证",
        "身份证号",
        "身份证号码",
        "证件号",
        "证件号码",
        "phone",
        "phoneno",
        "mobile",
        "tel",
        "telephone",
        "电话",
        "联系电话",
        "手机",
        "手机号",
        "联系方式",
        "address",
        "地址",
        "住址",
        "家庭住址",
        "现住址",
        "contact",
        "联系人",
        "紧急联系人",
        "gender",
        "性别",
        "birthdate",
        "出生日期",
        "出生年月",
        "age",
        "年龄",
        "bankcard",
        "银行卡",
        "卡号",
        "email",
        "邮箱",
    }
)


def _normalize_field_name(name: str) -> str:
    """字段名归一：去空白与下划线、转小写。"""
    return re.sub(r"[\s_\-]+", "", str(name or "")).lower()


def is_sensitive_field(name: str) -> bool:
    """字段名是不是患者可识别字段。"""
    normalized = _normalize_field_name(name)
    if not normalized:
        return False
    if normalized in SENSITIVE_FIELD_NAMES:
        return True
    # 英文名带前缀也算（如 ``patient_name``、``home_address``）。
    return any(
        normalized.startswith(prefix)
        for prefix in ("patient", "idcard", "home", "contact")
    )


def desensitize_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    """字段级脱敏：敏感字段整个丢掉，其余字符串值过一遍文本遮蔽。"""
    result: dict[str, Any] = {}
    for key, value in values.items():
        if is_sensitive_field(key):
            continue
        result[key] = desensitize_text(value) if isinstance(value, str) else value
    return result


# ---------------------------------------------------------------- 文本级

PLACEHOLDER_ID_CARD = "【身份证号已隐去】"
PLACEHOLDER_BANK_CARD = "【卡号已隐去】"
PLACEHOLDER_PHONE = "【电话已隐去】"
PLACEHOLDER_EMAIL = "【邮箱已隐去】"
PLACEHOLDER_NAME = "【姓名已隐去】"

#: (占位符, 识别模式)。顺序即应用顺序：身份证（15/18 位）要先于卡号（16–19 位），
#: 否则 18 位身份证会被当成卡号遮掉，占位符就不对了。
_MASKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (PLACEHOLDER_ID_CARD, re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])")),
    (PLACEHOLDER_ID_CARD, re.compile(r"(?<![0-9A-Za-z])\d{15}(?![0-9A-Za-z])")),
    (PLACEHOLDER_BANK_CARD, re.compile(r"(?<![0-9])\d{16,19}(?![0-9])")),
    (PLACEHOLDER_PHONE, re.compile(r"(?<![0-9])1[3-9]\d{9}(?![0-9])")),
    (PLACEHOLDER_PHONE, re.compile(r"(?<![0-9])0\d{2,3}-\d{7,8}(?![0-9])")),
    (PLACEHOLDER_EMAIL, re.compile(r"[0-9A-Za-z._%+-]+@[0-9A-Za-z.-]+\.[A-Za-z]{2,}")),
)

#: 带提示词的姓名（「患者张三」「姓名：李四」）。提示词后跟 2–4 个汉字。
_NAME_CUE = re.compile(
    r"(?P<label>姓名|患者|病人|受检者|家属|联系人)"
    r"\s*[:：]?\s*(?P<name>[一-龥]{2,4})"
)

#: 裸姓名（高频姓氏起头）。姓氏表刻意保守：收了「石白于余金方苏高田」这类
#: 容易在业务词里撞车的字（石蜡、白细胞、由于、其余、金黄、方形、苏木、高分、
#: 甲状腺），认错就把部位描述抹了。
_SURNAMES = (
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何林罗郑梁谢宋唐许韩冯邓曹彭曾肖董袁潘"
    "蒋蔡杜叶程魏吕丁任沈姚卢姜崔钟谭陆汪范廖贾夏韦付邹孟熊秦邱江尹薛段雷侯"
    "龙史陶黎贺顾毛郝龚邵万钱严武戴莫孔向汤"
)

_BARE_NAME = re.compile(rf"(?<![一-龥])([{_SURNAMES}][一-龥]{{1,2}})(?![一-龥])")

#: 标点后紧跟的姓名（「（李四取材）」这类夹在括号里的）。有标点打底，误伤
#: 业务词的概率低 —— 只取姓氏 + 1 个字，多取会把后面的业务词也吃掉。
_NAME_AFTER_PUNCT = re.compile(rf"(?<=[\(（\[【「『\"'“‘])([{_SURNAMES}][一-龥])")

#: 名字里不该出现的字：这些字一出现，多半是把业务词当成了姓名
#: （「患者为阳性」「由于取材…」「其余部位…」）。
_NAME_STOP_CHARS = frozenset(
    "为有无已未该本其所呈显阴阳强弱局全待即于与和或但因将对此则从自另每各于"
)

#: 职称：带职称的称呼不是姓名（「王主任」不遮）。
_TITLE_SUFFIXES: tuple[str, ...] = (
    "主任医师",
    "副主任",
    "主任",
    "医师",
    "医生",
    "护士",
    "大夫",
    "先生",
    "女士",
    "同志",
    "教授",
    "院长",
    "技师",
)


def _looks_like_name(candidate: str) -> bool:
    """这段文字像不像姓名。"""
    if not candidate:
        return False
    if any(char in _NAME_STOP_CHARS for char in candidate):
        return False
    for title in _TITLE_SUFFIXES:
        if candidate.endswith(title):
            return False
    return True


def _replace_cued_name(match: re.Match[str]) -> str:
    """带提示词的姓名：像姓名才遮，不像就原样留下。"""
    name = match.group("name")
    if not _looks_like_name(name):
        return match.group(0)
    return f"{match.group('label')}{PLACEHOLDER_NAME}"


def _replace_bare_name(match: re.Match[str]) -> str:
    """裸姓名：像姓名才遮，不像就原样留下。"""
    name = match.group(1)
    if not _looks_like_name(name):
        return match.group(0)
    return PLACEHOLDER_NAME


def desensitize_text(text: str | None) -> str:
    """把一段自由文本里的可识别信息遮蔽掉。

    ``None`` 给空串；本来就干净的文本原样返回（遮蔽是幂等的）。
    """
    if text is None:
        return ""
    result = str(text)
    if not result:
        return result
    for placeholder, pattern in _MASKERS:
        result = pattern.sub(placeholder, result)
    result = _NAME_CUE.sub(_replace_cued_name, result)
    result = _BARE_NAME.sub(_replace_bare_name, result)
    result = _NAME_AFTER_PUNCT.sub(_replace_bare_name, result)
    return result
