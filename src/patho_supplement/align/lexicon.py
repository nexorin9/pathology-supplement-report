"""部位同义词典（格式、加载、内置最小词表回退）。

**为什么需要这一层**：同一部位在不同人笔下是同一个部位。基线写「子宫体」，
回报写「宫体」；基线写「乳腺」，回报写「乳腺腺体」；基线写「宫颈」，
回报写「子宫颈」。字面不等就判「对不上」，结果是**明明是同一部位，却挂起
等人补** —— 病理科一天挂几十条，技术员的时间全花在「这俩说的是不是一处」。

向量相似度给不了确定性答案：0.87 分既可能是「宫体」也可能是「宫颈」，而
**猜错一次就是一份签出去却写错部位的补充报告**。所以部位别名必须**显式建模**
成一张可维护的词表 —— 每一个别名指向哪个规范词，是病理科自己说了算的事，
不是模型猜出来的事。词典能改、能审、能追溯，这才是敢承诺「错绑定数 = 0」
的前提。

**词典格式**（外置 JSON，一行一条规范词 + 别名列表 + 可选科室限定）::

    {
      "version": "2026.10",
      "entries": [
        {"canonical": "子宫体", "aliases": ["宫体", "内膜"], "departments": ["妇科"]},
        {"canonical": "乳腺",   "aliases": ["乳腺组织", "乳腺腺体"]}
      ]
    }

- ``canonical``：规范词，必填，非空字符串；
- ``aliases``：别名列表，可空；别名折算到规范词参与打分；
- ``departments``：科室限定，可空。**空 = 全院通用**；非空 = 只有该科室的
  病例才应用这条别名（详见 :meth:`Lexicon.canonicalize` 的取舍规则）；
- ``version``：词典版本标识，随词典一起改 —— 对齐结果的可复现性取决于
  「当时生效的是哪一版词典」，所以版本必须随运行摘要一起留痕。

**加载失败一律回退，不中断主流程**：词典文件缺失、JSON 非法、条目结构不对、
整份词典一条可用项都没有 —— 这四种情况都回退到**内置最小词表**（十余组常见
取材部位别名）并留下告警。理由很直接：一份报告的补充草稿不能因为「词表文件
被同事改坏了」就整批出不来；但「现在用的是回退词表」这件事必须让人看见，
否则科室会以为自己在用自己维护的词表。告警同时进两条去处：日志（运维看得见）
与词典对象自身（写进运行摘要可查）。

**与源产品的关系**：源项目的检索后端是 embedding 相似度检索
（``github_ref/haystack/haystack/components/retrievers/text_embedding_retriever.py``、
``.../retrievers/in_memory/embedding_retriever.py``），既没有中文词表，也没有
词法打分 —— 相似度既说不出「这条就是 B3 块」，也说不清「宫体归哪个规范词」。
本模块是替换掉向量后端的第一层：把部位别名显式建成可审计的词表，由确定性
的词法打分接手（见 :mod:`patho_supplement.align.scorer`）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .normalizer import normalize_region_desc

__all__ = [
    "BUILTIN_VERSION",
    "Lexicon",
    "LexiconEntry",
    "LexiconError",
    "builtin_entries",
    "builtin_lexicon",
    "load_lexicon",
]

logger = logging.getLogger(__name__)

#: 内置最小词表的版本标识。
BUILTIN_VERSION = "builtin-2026.10"

#: 内置最小词表来源标识。
BUILTIN_SOURCE = "内置最小词表"


class LexiconError(ValueError):
    """词典文件本身不可用（缺失 / 非法 JSON / 结构不对）。

    加载器捕获它并回退到内置词表，因此这个异常**不会**冒到主流程 ——
    它只在直接调用 :func:`load_lexicon` 且显式要求严格模式时才抛出。
    """


@dataclass(frozen=True)
class LexiconEntry:
    """一条词典条目：规范词 + 别名列表 + 可选科室限定。"""

    canonical: str
    aliases: tuple[str, ...] = ()
    departments: tuple[str, ...] = ()

    @property
    def canonical_key(self) -> str:
        """规范词的归一化形式（比较与建索引用）。"""
        return normalize_region_desc(self.canonical) or ""

    @property
    def alias_keys(self) -> tuple[str, ...]:
        """别名的归一化形式。"""
        keys: list[str] = []
        for alias in self.aliases:
            key = normalize_region_desc(alias)
            if key and key not in keys:
                keys.append(key)
        return tuple(keys)

    @property
    def restricted(self) -> bool:
        """是否带科室限定。"""
        return bool(self.departments)

    def applies_to(self, department: str | None) -> bool:
        """这条条目在给定科室下是否适用。

        - 未限定科室的条目：**任何科室都适用**；
        - 限定了科室的条目：只有科室对得上才适用；科室未知时**不适用** ——
          拿「妇科专用别名」去套消化科病例，是把一个错误换成一个更大的错误。
        """
        if not self.departments:
            return True
        if not department:
            return False
        return department in self.departments


@dataclass(frozen=True)
class Lexicon:
    """一部已装载的部位同义词典。

    不可变：装载之后不再改。词典要改就改文件、重新装载，并在运行摘要里
    留下新版本号 —— 半路改一个已经算过分的词典，等于让同一批回报算两次
    得出两个结果。
    """

    entries: tuple[LexiconEntry, ...] = ()
    version: str = BUILTIN_VERSION
    source: str = BUILTIN_SOURCE
    warnings: tuple[str, ...] = ()

    #: 归一化词形 → 命中条目。建在装载期，避免每次查询都重算。
    _index: Mapping[str, tuple[LexiconEntry, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """没显式给索引时按条目自动建一份 —— 手工构造词典也不会查不到。"""
        if not self._index:
            object.__setattr__(self, "_index", _build_index(self.entries))

    # ------------------------------------------------------------ 计数

    @property
    def entry_count(self) -> int:
        """词典条目数（规范词条数）。"""
        return len(self.entries)

    @property
    def alias_count(self) -> int:
        """别名总数（去重后按条目累加）。"""
        return sum(len(entry.alias_keys) for entry in self.entries)

    # ------------------------------------------------------------ 查询

    def _candidates(self, key: str, department: str | None) -> tuple[LexiconEntry, ...]:
        """查某个归一化词形的候选条目。

        取舍顺序：**通用条目优先于科室限定条目**；同类候选按规范词排序，
        保证同一个词在任何时候都解析到同一个规范词（结果可复现）。
        """
        hits = self._index.get(key, ())
        if not hits:
            return ()
        applicable = [entry for entry in hits if entry.applies_to(department)]
        if not applicable:
            return ()
        applicable.sort(key=lambda entry: (entry.restricted, entry.canonical_key))
        return tuple(applicable)

    def canonicalize(self, text: str | None, *, department: str | None = None) -> str | None:
        """把部位写法折算到规范词（归一化后）。

        查不到、输入为空、或只命中科室不匹配的限定条目时返回 ``None`` ——
        **查不到就是查不到**，不许猜一个最像的规范词塞进去。

        ``department`` 给了才可能命中科室限定条目；没给时只认全院通用条目。
        """
        key = normalize_region_desc(text)
        if not key:
            return None
        candidates = self._candidates(key, department)
        if not candidates:
            return None
        return candidates[0].canonical_key

    def canonical_label(self, text: str | None, *, department: str | None = None) -> str | None:
        """规范词的原始写法（用于展示与审计，不做归一化）。"""
        key = normalize_region_desc(text)
        if not key:
            return None
        candidates = self._candidates(key, department)
        if not candidates:
            return None
        return candidates[0].canonical

    def variants(
        self, text: str | None, *, department: str | None = None
    ) -> tuple[str, ...]:
        """该写法对应的全部参与打分的词形（归一化后）：规范词 + 它的别名。

        别名折算到规范词参与打分 —— 打分器拿这组词形与候选部位求相关度，
        别名命中规范词时得分高于无关词。

        查不到时返回空元组（该写法不参与词典扩展，打分器按原样处理）。
        """
        key = normalize_region_desc(text)
        if not key:
            return ()
        candidates = self._candidates(key, department)
        if not candidates:
            return ()
        entry = candidates[0]
        words: list[str] = []
        for word in (entry.canonical_key, *entry.alias_keys):
            if word and word not in words:
                words.append(word)
        return tuple(words)

    def is_alias(self, text: str | None, *, department: str | None = None) -> bool:
        """该写法是不是某个规范词的别名（而不是它本身就是规范词）。"""
        key = normalize_region_desc(text)
        if not key:
            return False
        candidates = self._candidates(key, department)
        if not candidates:
            return False
        entry = candidates[0]
        return key in entry.alias_keys and key != entry.canonical_key

    # ------------------------------------------------------------ 摘要

    def summary(self) -> dict[str, object]:
        """词典的运行摘要视图（写进运行摘要可查）。"""
        return {
            "version": self.version,
            "source": self.source,
            "entry_count": self.entry_count,
            "alias_count": self.alias_count,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------- 内置词表


def _entry(canonical: str, aliases: Sequence[str] = (), departments: Sequence[str] = ()) -> LexiconEntry:
    """构造一条内置条目（去掉空白项）。"""
    return LexiconEntry(
        canonical=canonical.strip(),
        aliases=tuple(a.strip() for a in aliases if a and a.strip()),
        departments=tuple(d.strip() for d in departments if d and d.strip()),
    )


#: 内置最小词表：常见取材部位别名 —— 规范词 → 别名。
#:
#: 词表是**最小可用集**而非全量解剖学词表：只收「院内真的会出现两种写法」
#: 的部位，让科室在此基础上按自己的登记习惯增补。带科室限定的条目示范
#: 「这条别名只在本科室成立」的用法。
BUILTIN_ENTRIES: tuple[LexiconEntry, ...] = (
    _entry("子宫体", ("宫体", "内膜", "子宫体部")),
    _entry("宫颈", ("子宫颈", "宫颈部", "宫颈组织")),
    _entry("卵巢", ("卵巢组织", "卵巢实质", "卵泡")),
    _entry("输卵管", ("输卵管管", "输卵管组织")),
    _entry("乳腺", ("乳腺组织", "乳腺腺体", "乳腺实质")),
    _entry("甲状腺", ("甲状腺组织", "甲状组织")),
    _entry("淋巴结", ("淋巴结组织", "淋巴结构", "淋巴结窦")),
    _entry("胃", ("胃壁", "胃黏膜", "胃组织")),
    _entry("食管", ("食管壁", "食管黏膜")),
    _entry("小肠", ("小肠壁", "小肠黏膜")),
    _entry("结肠", ("结肠壁", "结肠黏膜", "结肠组织")),
    _entry("直肠", ("直肠壁", "直肠黏膜")),
    _entry("阑尾", ("阑尾壁", "尾蚓突")),
    _entry("肝", ("肝脏", "肝组织", "肝实质")),
    _entry("胆囊", ("胆囊壁", "胆囊组织")),
    _entry("胰腺", ("胰腺组织", "胰腺实质")),
    _entry("肺", ("肺组织", "肺叶", "肺实质")),
    _entry("胸膜", ("胸膜壁", "壁层胸膜"), ("胸外科",)),
    _entry("肾", ("肾脏", "肾组织", "肾实质")),
    _entry("膀胱", ("膀胱壁", "膀胱黏膜")),
    _entry("前列腺", ("前列腺组织", "前列腺实质")),
    _entry("皮肤", ("皮肤组织", "表皮", "真皮")),
    _entry("骨", ("骨组织", "骨质", "骨组织块")),
    _entry("宫腔", ("宫腔内容物", "宫腔组织"), ("妇科",)),
)


def builtin_entries() -> tuple[LexiconEntry, ...]:
    """内置最小词表条目（拷贝一份，外部改不动内置表）。"""
    return tuple(BUILTIN_ENTRIES)


def _build_index(entries: Iterable[LexiconEntry]) -> dict[str, tuple[LexiconEntry, ...]]:
    """建归一化词形 → 候选条目 的索引。"""
    buckets: dict[str, list[LexiconEntry]] = {}
    for entry in entries:
        keys = [entry.canonical_key, *entry.alias_keys]
        for key in keys:
            if not key:
                continue
            bucket = buckets.setdefault(key, [])
            if entry not in bucket:
                bucket.append(entry)
    return {key: tuple(items) for key, items in buckets.items()}


def builtin_lexicon() -> Lexicon:
    """内置最小词表（不带告警）。"""
    entries = builtin_entries()
    return Lexicon(
        entries=entries,
        version=BUILTIN_VERSION,
        source=BUILTIN_SOURCE,
        warnings=(),
        _index=_build_index(entries),
    )


# ---------------------------------------------------------------- 加载


def _parse_entries(payload: object) -> tuple[list[LexiconEntry], list[str]]:
    """从已解析的 JSON 负载里取出条目。

    返回 ``(条目列表, 告警列表)``。**单条非法只跳过这一条**并留下告警 ——
    一条写错的别名不该把整份词典作废；但一条都留不下时由调用方回退。
    """
    if isinstance(payload, Mapping):
        raw_entries = payload.get("entries")
    elif isinstance(payload, list):
        raw_entries = payload
    else:
        raise LexiconError("词典根节点必须是对象（含 entries 字段）或数组")

    if not isinstance(raw_entries, list):
        raise LexiconError("词典 entries 字段必须是数组")

    entries: list[LexiconEntry] = []
    warnings: list[str] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, Mapping):
            warnings.append(f"第 {index + 1} 条不是对象，已跳过")
            continue
        canonical = raw.get("canonical")
        if not isinstance(canonical, str) or not canonical.strip():
            warnings.append(f"第 {index + 1} 条缺 canonical（规范词），已跳过")
            continue
        aliases_raw = raw.get("aliases") or []
        if not isinstance(aliases_raw, list):
            warnings.append(f"第 {index + 1} 条 aliases 不是数组，已按空别名处理")
            aliases_raw = []
        aliases = tuple(
            a.strip() for a in aliases_raw if isinstance(a, str) and a.strip()
        )
        departments_raw = raw.get("departments") or []
        if not isinstance(departments_raw, list):
            warnings.append(f"第 {index + 1} 条 departments 不是数组，已按不限科室处理")
            departments_raw = []
        departments = tuple(
            d.strip() for d in departments_raw if isinstance(d, str) and d.strip()
        )
        entries.append(
            LexiconEntry(
                canonical=canonical.strip(), aliases=aliases, departments=departments
            )
        )
    return entries, warnings


def _load_from_file(path: Path) -> tuple[tuple[LexiconEntry, ...], str, list[str]]:
    """从外置 JSON 文件装载条目与版本。"""
    if not path.exists():
        raise LexiconError(f"词典文件不存在：{path}")
    if not path.is_file():
        raise LexiconError(f"词典路径不是文件：{path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LexiconError(f"词典文件读取失败：{path}（{exc}）") from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise LexiconError(f"词典文件不是合法 JSON：{path}（第 {exc.lineno} 行 {exc.msg}）") from exc

    entries, warnings = _parse_entries(payload)
    if not entries:
        raise LexiconError(f"词典文件里一条可用条目都没有：{path}")

    version = ""
    if isinstance(payload, Mapping):
        raw_version = payload.get("version")
        if isinstance(raw_version, str):
            version = raw_version.strip()
    return tuple(entries), version or f"file:{path.name}", warnings


def load_lexicon(
    path: Path | str | None = None, *, strict: bool = False
) -> Lexicon:
    """装载部位同义词典。

    ``path`` 为 ``None`` 时直接用内置最小词表。给了路径则读外置 JSON；
    **装载失败一律回退到内置词表并留下告警**，主流程不中断
    （``strict=True`` 时改为抛 :class:`LexiconError`，供自检与测试使用）。

    告警同时走两条去处：``logging`` 告警（运维在日志里看得见）与
    ``Lexicon.warnings``（写进运行摘要可查）。
    """
    if path is None:
        return builtin_lexicon()

    file_path = Path(path).expanduser()
    try:
        entries, version, warnings = _load_from_file(file_path)
    except LexiconError as exc:
        if strict:
            raise
        message = f"{exc}；已回退到{BUILTIN_SOURCE}"
        logger.warning("部位同义词典装载失败：%s", message)
        fallback = builtin_lexicon()
        return Lexicon(
            entries=fallback.entries,
            version=fallback.version,
            source=fallback.source,
            warnings=(message,),
            _index=fallback._index,
        )

    lexicon = Lexicon(
        entries=entries,
        version=version,
        source=str(file_path),
        warnings=tuple(warnings),
        _index=_build_index(entries),
    )
    for warning in warnings:
        logger.warning("部位同义词典：%s", warning)
    return lexicon
