"""PEP 440 版本号解析与版本范围（specifier）匹配。

仅依赖标准库，覆盖本服务需要的 PEP 440 子集：

版本：``N(.N)*`` 后接可选的
  - 预发布：aN / bN / rcN（alpha/beta/pre/c/preview 等价别名）
  - 后发布：postN（rev/- 等价别名）
  - 开发版：devN
  - 纪元前缀：``N!``

范围操作符：``==``、``!=``、``<``、``<=``、``>``、``>=``、``~=``、``===``，
``==`` / ``!=`` 支持 ``.*`` 前缀匹配与本地版本前缀匹配；逗号表示交集。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import List, Sequence, Tuple

__all__ = ["Version", "Specifier", "SpecifierSet", "InvalidVersion", "InvalidSpecifier"]


class InvalidVersion(ValueError):
    """版本字符串不符合 PEP 440。"""


class InvalidSpecifier(ValueError):
    """版本范围字符串不合法。"""


_RELEASE = r"(\d+(?:\.\d+)*)"
_PRE = r"(?:[-_.]?(a|b|c|rc|alpha|beta|pre|preview)[-_.]?(\d*))?"
_POST = r"(?:(?:-(?=\d))|(?:[-_.]?(post|rev|r)[-_.]?(\d*)))?"
_DEV = r"(?:[-_.]?(dev)[-_.]?(\d*))?"
_LOCAL = r"(?:\+([a-z0-9]+(?:[._-][a-z0-9]+)*))?"
VERSION_RE = re.compile(
    rf"^\s*v?(?:(\d+)!)?{_RELEASE}{_PRE}{_POST}(?:\.\d+)?{_DEV}{_LOCAL}\s*$",
    re.IGNORECASE,
)
# 允许 post 分隔变体 "1.0-1"：与上面重复的 dev 位置需要单独兼容，做一个宽松二次解析。
VERSION_RE_LOOSE = re.compile(
    r"""^\s*v?
    (?:(?P<epoch>\d+)!)?
    (?P<release>\d+(?:\.\d+)*)
    (?:[._-]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[._-]?(?P<pre_n>\d*))?
    (?:(?:-(?P<post_n2>\d+))|(?:[._-]?(?:post|rev|r)[._-]?(?P<post_n>\d*)))?
    (?:[._-]?dev[._-]?(?P<dev_n>\d*))?
    (?:\+(?P<local>[a-z0-9]+(?:[._-][a-z0-9]+)*))?
    \s*$""",
    re.IGNORECASE | re.VERBOSE,
)

_PRE_ALIASES = {
    "a": "a", "alpha": "a",
    "b": "b", "beta": "b",
    "c": "rc", "rc": "rc", "pre": "rc", "preview": "rc",
}
_PRE_ORDER = {"a": 0, "b": 1, "rc": 2}


def _num(s: str | None) -> int:
    if not s:
        return 0
    return int(s)


@total_ordering
@dataclass(frozen=True)
class Version:
    raw: str
    epoch: int
    release: Tuple[int, ...]
    pre: Tuple[str, int] | None
    post: int | None
    dev: int | None
    local: str | None

    @classmethod
    def parse(cls, text: str) -> "Version":
        if not isinstance(text, str):
            raise InvalidVersion(f"版本必须是字符串: {text!r}")
        m = VERSION_RE_LOOSE.match(text)
        if not m:
            raise InvalidVersion(f"非法 PEP 440 版本号: {text!r}")
        local = m.group("local")
        release = tuple(int(x) for x in m.group("release").split("."))
        pre_l = m.group("pre_l")
        pre: Tuple[str, int] | None = None
        if pre_l:
            label = _PRE_ALIASES[pre_l.lower()]
            pre = (label, _num(m.group("pre_n")))
        post = m.group("post_n")
        post_n: int | None
        if post is not None:
            post_n = _num(post)
        elif m.group("post_n2") is not None:
            post_n = int(m.group("post_n2"))
        else:
            post_n = None
        dev = m.group("dev_n")
        dev_n = _num(dev) if dev is not None and m.group("dev_n") is not None else None
        if m.group("dev_n") is None and re.search(r"dev", text, re.IGNORECASE):
            dev_n = 0
        return cls(
            raw=text.strip(),
            epoch=int(m.group("epoch") or 0),
            release=release,
            pre=pre,
            post=post_n,
            dev=dev_n,
            local=local.lower() if local else None,
        )

    @property
    def public(self) -> str:
        return str(self)

    @property
    def base_version(self) -> str:
        return f"{(str(self.epoch) + '!') if self.epoch else ''}{'.'.join(map(str, self.release))}"

    def is_prerelease(self) -> bool:
        return self.dev is not None or self.pre is not None

    def is_devrelease(self) -> bool:
        return self.dev is not None

    def _key(self) -> Tuple:
        """与 PEP 440 一致的排序键：

        1.0.dev1 < 1.0a1.dev1 < 1.0a1 < 1.0b1 < 1.0rc1 < 1.0
                  < 1.0.post1.dev1 < 1.0.post1
        统一使用 4 段定长元组，避免 Python 前缀元组的比较陷阱。
        """
        release = self.release
        i = len(release)
        while i > 1 and release[i - 1] == 0:
            i -= 1
        release = release[:i]
        INF = 10**18
        if self.dev is not None and self.pre is None and self.post is None:
            phase = (0, 0, 0, self.dev)                       # 纯 dev
        elif self.dev is not None and self.pre is not None:
            phase = (1, _PRE_ORDER[self.pre[0]], self.pre[1], self.dev)  # 预发布的 dev
        elif self.pre is not None:
            phase = (1, _PRE_ORDER[self.pre[0]], self.pre[1], INF)       # 预发布
        elif self.dev is not None and self.post is not None:
            phase = (3, self.post, 0, self.dev)              # post 的 dev
        elif self.post is not None:
            phase = (3, self.post, 1, 0)                     # post
        else:
            phase = (2, 0, 0, 0)                             # 最终版
        return (self.epoch, release, phase, self.local or "")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __lt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        ka, kb = self._key(), other._key()
        if ka[0] != kb[0]:
            return ka[0] < kb[0]
        ra, rb = ka[1], kb[1]
        n = max(len(ra), len(rb))
        ra = ra + (0,) * (n - len(ra))
        rb = rb + (0,) * (n - len(rb))
        return (ra, ka[2], ka[3]) < (rb, kb[2], kb[3])

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        parts = []
        if self.epoch:
            parts.append(f"{self.epoch}!")
        parts.append(".".join(str(x) for x in self.release))
        if self.pre is not None:
            parts.append(f"{self.pre[0]}{self.pre[1]}")
        if self.post is not None:
            parts.append(f".post{self.post}")
        if self.dev is not None:
            parts.append(f".dev{self.dev}")
        if self.local:
            parts.append(f"+{self.local}")
        return "".join(parts)


_OP_RE = re.compile(r"^\s*(===|==|!=|~=|>=|<=|>|<)\s*(.+?)\s*$")


@dataclass(frozen=True)
class Specifier:
    op: str
    version: str  # 可能带 .* 或本地版本

    @classmethod
    def parse(cls, text: str) -> "Specifier":
        m = _OP_RE.match(text)
        if not m:
            raise InvalidSpecifier(f"非法版本约束: {text!r}")
        op, ver = m.group(1), m.group(2)
        if op != "===":
            # 校验版本侧（允许 .* 后缀）
            v = ver[:-2] if ver.endswith(".*") else ver
            try:
                parsed = Version.parse(v)
            except InvalidVersion:
                # 允许 ==1.0+local 这类；本地段上面也能解析
                raise InvalidSpecifier(f"约束 {text!r} 中的版本号不合法")
            if op == "~=" and len(parsed.release) < 2:
                raise InvalidSpecifier(f"~= 至少需要主.次两段版本: {text!r}")
            if ver.endswith(".*") and op not in ("==", "!="):
                raise InvalidSpecifier(f".* 前缀匹配只能配合 == / != 使用: {text!r}")
        else:
            if not ver:
                raise InvalidSpecifier(f"约束 {text!r} 中的版本号为空")
        return cls(op=op, version=ver)

    def _prefix_match(self, cand: Version) -> bool:
        assert self.version.endswith(".*")
        prefix_text = self.version[:-2]
        if "+" not in prefix_text and cand.local:
            # ==1.0.* 不应匹配 1.0+local？PEP 440 规定前缀匹配忽略本地版本，
            # 比较公开版本即可。
            pass
        pv = Version.parse(prefix_text)
        crel = cand.release[: len(pv.release)]
        prel = pv.release
        n = max(len(crel), len(prel))
        crel = crel + (0,) * (n - len(crel))
        prel = prel + (0,) * (n - len(prel))
        return cand.epoch == pv.epoch and crel == prel

    def contains(self, cand: Version, prereleases: bool = True) -> bool:
        if not prereleases and cand.is_prerelease():
            # 仅当约束本身指向预发布时才放行
            if not (self.op in ("==", "!=", "===") and
                    _spec_mentions_prerelease(self.version)):
                return False
        op, sv_text = self.op, self.version
        if op == "===":
            return cand.raw.strip() == sv_text.strip()
        if sv_text.endswith(".*"):
            # 合法性已在 parse() 中校验
            ok = self._prefix_match(cand)
            return ok if op == "==" else not ok
        sv = Version.parse(sv_text)
        # 比较公开版本（忽略本地版本差异）
        cpub = Version.parse(cand.base_version) if cand.local else cand
        spub = Version.parse(sv.base_version) if sv.local else sv
        if op == "==":
            if sv.local:
                return cand == sv
            return cpub == spub
        if op == "!=":
            if sv.local:
                return cand != sv
            return cpub != spub
        if op == ">=":
            return cpub >= spub
        if op == "<=":
            return cpub <= spub
        if op == ">":
            return cpub > spub
        if op == "<":
            return cpub < spub
        if op == "~=":
            rel = sv.release
            upper_release = rel[:-2] + (rel[-2] + 1,)
            upper = Version(
                raw="", epoch=sv.epoch, release=upper_release,
                pre=None, post=None, dev=None, local=None,
            )
            lower = Version(
                raw="", epoch=sv.epoch, release=rel,
                pre=sv.pre, post=sv.post, dev=sv.dev, local=None,
            )
            return cpub >= lower and cpub < upper
        raise InvalidSpecifier(f"未知操作符: {op}")

    def __str__(self) -> str:
        return f"{self.op}{self.version}"


def _spec_mentions_prerelease(text: str) -> bool:
    try:
        return Version.parse(text[:-2] if text.endswith(".*") else text).is_prerelease()
    except InvalidVersion:
        return False


@dataclass(frozen=True)
class SpecifierSet:
    specifiers: Tuple[Specifier, ...]

    @classmethod
    def parse(cls, text: str | None) -> "SpecifierSet":
        if text is None or not text.strip() or text.strip() == "*":
            return cls(())
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if not parts:
            return cls(())
        specs: List[Specifier] = []
        for p in parts:
            # 兼容裸版本/前缀："1.2" 与 "1.2.*" 等价于 "==1.2" / "==1.2.*"
            if not p[:1].isalpha() and not p.startswith(("=", "!", "<", ">", "~")):
                p = "==" + p
            specs.append(Specifier.parse(p))
        obj = cls(tuple(specs))
        obj.validate_nonempty()
        return obj

    def contains(self, v: Version, prereleases: bool = True) -> bool:
        return all(s.contains(v, prereleases) for s in self.specifiers)

    def filter(self, versions: Sequence[Version]) -> List[Version]:
        return [v for v in versions if self.contains(v)]

    def validate_nonempty(self, universe: Sequence[Version] | None = None) -> None:
        """范围自身必须有效：操作符合法；若给定候选集，至少要能命中一个版本。

        本服务发布时校验“范围语法有效”，故默认只做语法检查。
        """
        if universe is not None and not self.filter(list(universe)):
            raise InvalidSpecifier(
                f"版本范围 {self} 在候选 {sorted(universe)} 中无解"
            )

    def __bool__(self) -> bool:
        return bool(self.specifiers)

    def __str__(self) -> str:
        return ",".join(str(s) for s in self.specifiers)
