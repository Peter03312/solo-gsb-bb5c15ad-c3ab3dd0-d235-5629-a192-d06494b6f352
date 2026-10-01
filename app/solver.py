"""插件安装方案求解器。

输入某一目录修订（catalog revision）下的全部未撤销版本快照，按
“提交的运行环境 + 目标插件版本范围”求出完整安装方案。

设计要点
--------
* 每个插件只允许选中一个版本；所有传递依赖必须落在同一插件已选版本上，
  多个来源的范围取交集。
* 运行环境兼容：候选版本必须支持提交的每一个运行环境（名称 + 版本范围）。
* 互斥：候选版本声明的 conflicts 若与已选版本（任一方向）重合即剪除。
* 环：最终图必须是 DAG；检测到环立即剪枝，所有分支都含环时报 ``CycleError``。
* 确定性：候选按版本号降序尝试，变量选择按“可行候选数升序、插件名升序”，
  输出按插件名排序 —— 同一快照 + 同一请求必得同一方案。
* 冲突驱动回溯：分支矛盾时记录“肇事插件”，回溯到最近的肇事选择点，
  保证算法完备（不会因贪心漏解）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .version import SpecifierSet, Version

# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class RuntimeRequirement:
    name: str
    spec: SpecifierSet  # 空集合表示不限版本


@dataclass(frozen=True)
class Candidate:
    plugin: str
    version: Version
    runtimes: Tuple[RuntimeRequirement, ...]            # 该版本支持的运行环境
    dependencies: Tuple[Tuple[str, SpecifierSet], ...]  # (依赖插件, 范围)
    conflicts: Tuple[str, ...]                          # 互斥插件名（任意版本）


@dataclass(frozen=True)
class Snapshot:
    """某一目录修订下的只读快照（仅含未撤销版本）。"""
    revision: int
    candidates_by_plugin: Dict[str, Tuple[Candidate, ...]]

    def candidates_for(self, plugin: str) -> Tuple[Candidate, ...]:
        result = list(self.candidates_by_plugin.get(plugin, ()))
        result.sort(key=lambda c: c.version, reverse=True)
        return tuple(result)


class SolverError(Exception):
    """求解失败基类。``reasons`` 为结构化冲突说明。"""

    def __init__(self, message: str, reasons: Sequence[dict]):
        super().__init__(message)
        self.reasons = list(reasons)


class UnsatisfiableError(SolverError):
    """不存在满足全部约束的方案（409）。"""


class CycleError(SolverError):
    """所有可行选择都存在依赖环（422）。"""


@dataclass
class _State:
    snapshot: Snapshot
    runtimes: Tuple[RuntimeRequirement, ...]
    chosen: Dict[str, Candidate] = field(default_factory=dict)
    # plugin -> [(范围, 提出者插件)]；提出者为 None 表示用户目标
    constraints: Dict[str, List[Tuple[SpecifierSet, Optional[str]]]] = field(default_factory=dict)


class _Backtrack(BaseException):
    """内部控制信号：回溯到深度为 target 的选择帧重试。"""

    def __init__(self, target: int):
        super().__init__(target)
        self.target = target


# ---------------------------------------------------------------- 运行环境兼容

def _ranges_overlap(a: SpecifierSet, b: SpecifierSet) -> bool:
    """两个范围交集是否非空。

    范围均为有限个半开/闭半空间的交，其交集若非空，必在两侧边界版本之一
    或其紧邻整数偏移点处可取，因此用边界采样判定即可。
    """
    points: List[Version] = []
    for specset in (a, b):
        for s in specset.specifiers:
            v = s.version
            if v.endswith(".*"):
                v = v[:-2]
            try:
                points.append(Version.parse(v))
            except ValueError:
                pass
    candidates = list(points)
    for p in points:
        candidates.extend(_neighbours(p))
    for p in candidates:
        if a.contains(p) and b.contains(p):
            return True
    return not points  # 两侧均无边界（全空范围）视为重叠


def _neighbours(v: Version) -> List[Version]:
    rel = v.release
    out: List[Version] = []
    for i in range(len(rel)):
        up = list(rel)
        up[i] += 1
        out.append(_mk(up))
    if rel and rel[-1] > 0:
        down = list(rel)
        down[-1] -= 1
        out.append(_mk(down))
    return out


def _mk(release: List[int]) -> Version:
    return Version(raw="", epoch=0, release=tuple(release),
                   pre=None, post=None, dev=None, local=None)


def _runtime_ok(c: Candidate, runtimes: Sequence[RuntimeRequirement]) -> Optional[str]:
    """返回首个不兼容的运行环境名；全部兼容返回 None。"""
    for req in runtimes:
        ok = False
        for declared in c.runtimes:
            if declared.name != req.name:
                continue
            # 声明为空：支持该运行环境任意版本；请求为空：任意版本即可
            if not declared.spec.specifiers or not req.spec.specifiers:
                ok = True
                break
            if _ranges_overlap(declared.spec, req.spec):
                ok = True
                break
        if not ok:
            return req.name
    return None


# ---------------------------------------------------------------- 环检测

def _find_cycle(edges: Dict[str, List[str]]) -> Optional[List[str]]:
    """在有向图中找一个环，返回以字典序最小节点为起点旋转后的节点序列。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {n: WHITE for n in edges}
    stack: List[str] = []
    found: List[List[str]] = []

    def dfs(u: str) -> bool:
        color[u] = GRAY
        stack.append(u)
        for w in sorted(edges.get(u, [])):
            if color.get(w) == GRAY:
                found.append(stack[stack.index(w):] + [w])
                return True
            if color.get(w) == WHITE and dfs(w):
                return True
        stack.pop()
        color[u] = BLACK
        return False

    for n in sorted(edges):
        if color[n] == WHITE and dfs(n):
            cyc = found[0]
            body = cyc[:-1]
            k = min(range(len(body)), key=lambda i: body[i])
            rotated = body[k:] + body[:k]
            return rotated + [rotated[0]]
    return None


# ---------------------------------------------------------------- 主入口

def solve(
    snapshot: Snapshot,
    runtimes: Sequence[RuntimeRequirement],
    targets: Dict[str, SpecifierSet],
) -> Dict[str, Candidate]:
    """求完整方案；失败抛 :class:`UnsatisfiableError` 或 :class:`CycleError`。"""
    return _solve_engine(snapshot, runtimes, targets, baseline=None)


def solve_minimal_change(
    snapshot: Snapshot,
    runtimes: Sequence[RuntimeRequirement],
    targets: Dict[str, SpecifierSet],
    baseline_versions: Dict[str, Version],
) -> Dict[str, Candidate]:
    """在全部完整方案中求相对 ``baseline_versions`` 变动最少的一个。

    用于“历史方案预演”：新增（baseline 没有）、移除（baseline 有但新方案
    没有）、换版（同一插件版本不同）各计一次，保留计 0。变动数平手时保留
    :func:`solve` 的确定性结果 —— 引擎以相同的 MRV + 版本降序 DFS 枚举，
    平手分支保留第一个找到的完整方案。
    """
    return _solve_engine(
        snapshot, runtimes, targets, baseline=dict(baseline_versions))


def _solve_engine(
    snapshot: Snapshot,
    runtimes: Sequence[RuntimeRequirement],
    targets: Dict[str, SpecifierSet],
    baseline: Optional[Dict[str, Version]],
) -> Dict[str, Candidate]:
    """求解引擎。

    ``baseline is None``：经典模式，返回 DFS 找到的第一个完整方案。
    ``baseline`` 给定时：分支定界枚举全部完整方案，返回变动数最小者。
    """
    st = _State(snapshot=snapshot, runtimes=tuple(runtimes))
    reasons: List[dict] = []
    cycle_seen: List[List[str]] = []
    choice_depth: Dict[str, int] = {}
    # 最小变动模式的当前最优；经典模式不使用
    best: List[Optional[Dict[str, Candidate]]] = [None]
    best_cost: List[int] = [10**18]
    # 经典模式的第一个完整方案
    first: List[Optional[Dict[str, Candidate]]] = [None]

    for plugin in sorted(targets):
        st.constraints.setdefault(plugin, []).append((targets[plugin], None))

    def add_reason(r: dict) -> None:
        if not any(x == r for x in reasons):
            reasons.append(r)

    def feasible_candidates(plugin: str) -> List[Candidate]:
        specs = [s for s, _ in st.constraints.get(plugin, [])]
        out: List[Candidate] = []
        for c in snapshot.candidates_for(plugin):
            if not all(s.contains(c.version) for s in specs):
                continue
            if _runtime_ok(c, st.runtimes) is not None:
                continue
            out.append(c)
        return out

    def conflicts_with(a: Candidate, b: Candidate) -> bool:
        return b.plugin in a.conflicts or a.plugin in b.conflicts

    def full_cost(plan: Dict[str, Candidate]) -> int:
        """完整方案相对 baseline 的变动插件数（新增/换版/移除各 1）。"""
        total = 0
        for p, cand in plan.items():
            old = baseline.get(p) if baseline is not None else None
            if old is None or cand.version != old:
                total += 1
        if baseline is not None:
            total += sum(1 for p in baseline if p not in plan)
        return total

    def lower_bound() -> int:
        """当前前缀上单调的变动数下界。

        只计入沿搜索延伸不可能再减少的部分：已选插件中的新增/换版，
        以及已进入约束集合必被选入、但 baseline 中不存在的插件（必新增）。
        baseline 中尚未进入约束的插件不计入（未来可能被保留，计入会破坏单调性）。
        """
        lb = 0
        for p, cand in st.chosen.items():
            old = baseline.get(p)
            if old is None or cand.version != old:
                lb += 1
        for p in st.constraints:
            if p not in st.chosen and p not in baseline:
                lb += 1
        return lb

    def contradiction():
        """检查已选集合上的范围矛盾与互斥。

        返回 (原因列表, 肇事帧深度集合)：肇事帧指“做出后可撤销该矛盾”的
        最近选择点；回溯到其中最深的帧即可剪枝。
        """
        local: List[dict] = []
        depths: Set[int] = set()
        for p, cand in list(st.chosen.items()):
            specs = [s for s, _ in st.constraints.get(p, [])]
            if not all(s.contains(cand.version) for s in specs):
                sources = {src for _, src in st.constraints.get(p, [])
                           if src in st.chosen}
                # 撤销任一“提出冲突约束”的帧或“选了该版本”的帧都可解除矛盾
                for src in sources | {p}:
                    depths.add(choice_depth[src])
                local.append({
                    "type": "version_conflict",
                    "plugin": p,
                    "selected": str(cand.version),
                    "required_by": [
                        {"plugin": src, "spec": str(spec)}
                        for spec, src in st.constraints.get(p, [])
                    ],
                })
        items = sorted(st.chosen.items())
        for i, (a_name, a) in enumerate(items):
            for b_name, b in items[i + 1:]:
                if conflicts_with(a, b):
                    depths.add(choice_depth[a_name])
                    depths.add(choice_depth[b_name])
                    local.append({
                        "type": "mutex_conflict",
                        "plugins": [a_name, b_name],
                        "versions": [str(a.version), str(b.version)],
                    })
        return (local, depths) if local else None

    def detect_cycle() -> Optional[List[str]]:
        edges: Dict[str, List[str]] = {p: [] for p in st.chosen}
        for p, cand in st.chosen.items():
            for dep, _ in cand.dependencies:
                if dep in st.chosen:
                    edges[p].append(dep)
        return _find_cycle(edges)

    def apply_choice(depth: int, plugin: str, cand: Candidate) -> List[str]:
        st.chosen[plugin] = cand
        choice_depth[plugin] = depth
        added: List[str] = []
        for dep, spec in cand.dependencies:
            st.constraints.setdefault(dep, []).append((spec, plugin))
            added.append(dep)
        return added

    def rollback(plugin: str, added: List[str]) -> None:
        st.chosen.pop(plugin, None)
        choice_depth.pop(plugin, None)
        for dep in added:
            lst = st.constraints.get(dep)
            if not lst:
                continue
            for i in range(len(lst) - 1, -1, -1):
                if lst[i][1] == plugin:
                    lst.pop(i)
                    break
            if not lst:
                st.constraints.pop(dep, None)

    def recurse(depth: int) -> Optional[Dict[str, Candidate]]:
        contra = contradiction()
        if contra:
            rs, depths = contra
            for r in rs:
                add_reason(r)
            # 返回到最深肇事帧重试：用异常携带深度向上弹栈
            raise _Backtrack(max(depths))

        unassigned = sorted(p for p in st.constraints if p not in st.chosen)
        if not unassigned:
            cyc = detect_cycle()
            if cyc:
                cycle_seen.append(cyc)
                add_reason({
                    "type": "dependency_cycle",
                    "cycle": cyc,
                    "selection": {p: str(c.version) for p, c in st.chosen.items()},
                })
                # 环上最深的选择帧是最近的可解除点
                raise _Backtrack(max(choice_depth[p] for p in cyc[:-1]))
            plan = dict(st.chosen)
            if baseline is None:
                first[0] = plan
                return plan
            # 最小变动模式：分支定界。首个完整方案即确定性参考方案，
            # 只有变动数严格更小时才替换 —— 平手保留先找到者。
            cost = full_cost(plan)
            if cost < best_cost[0]:
                best[0] = plan
                best_cost[0] = cost
            raise _Backtrack(depth - 1)  # 继续枚举其余完整方案

        if baseline is not None and lower_bound() > best_cost[0]:
            # 下界已不可能优于当前最优；剪到本插件的上一选择帧换候选
            raise _Backtrack(depth - 1)

        # MRV：可行候选最少的插件先选，平手按插件名
        pending = sorted(
            ((len(feasible_candidates(p)), p) for p in unassigned),
            key=lambda x: (x[0], x[1]),
        )
        _, plugin = pending[0]
        feasible = feasible_candidates(plugin)

        if not feasible:
            no_candidate(plugin)
            # 回溯到最深的“提出该插件约束”的选择帧；若是用户目标则根无解
            target = no_candidate_target(plugin, depth)
            raise _Backtrack(target)

        for cand in feasible:  # 已按版本降序
            added = apply_choice(depth, plugin, cand)
            try:
                result = recurse(depth + 1)
            except _Backtrack as bt:
                rollback(plugin, added)
                if bt.target < depth:
                    # 矛盾解除点在更早的帧，本插件其余候选无意义
                    raise
                # bt.target == depth：尝试本插件下一候选
                continue
            rollback(plugin, added)
            return result
        # 所有候选都试过仍失败：回到上一帧
        raise _Backtrack(depth - 1)

    def no_candidate(plugin: str) -> None:
        cs = snapshot.candidates_for(plugin)
        specs = st.constraints.get(plugin, [])
        if not cs:
            add_reason({
                "type": "missing_plugin",
                "plugin": plugin,
                "required_by": [
                    {"plugin": src, "spec": str(spec)} for spec, src in specs
                ],
            })
        else:
            runtime_blocked = [
                req.name for req in st.runtimes
                if all(_runtime_ok(c, [req]) is not None for c in cs)
            ]
            add_reason({
                "type": "no_matching_version",
                "plugin": plugin,
                "available": [str(c.version) for c in cs],
                "required_by": [
                    {"plugin": src, "spec": str(spec)} for spec, src in specs
                ],
                "runtime_blocked": sorted(set(runtime_blocked)),
            })

    def no_candidate_target(plugin: str, depth: int) -> int:
        sources = {src for _, src in st.constraints.get(plugin, [])
                   if src in choice_depth}
        if not sources:
            return -1  # 约束直接来自用户目标，根上即无解
        return max(choice_depth[src] for src in sources)

    try:
        recurse(0)
    except _Backtrack:
        pass

    if baseline is not None and best[0] is not None:
        return best[0]
    if baseline is None and first[0] is not None:
        return first[0]

    if cycle_seen:
        non_cycle = [r for r in reasons if r["type"] != "dependency_cycle"]
        cyc = min(cycle_seen, key=lambda c: c)
        msg = f"检测到依赖环: {' -> '.join(cyc)}"
        if non_cycle:
            msg += "（同时存在其它冲突）"
        raise CycleError(msg, [{"type": "dependency_cycle", "cycle": cyc}] + non_cycle)
    raise UnsatisfiableError("不存在满足全部约束的安装方案", _dedup(reasons))


def _dedup(reasons: List[dict]) -> List[dict]:
    seen, out = set(), []
    for r in reasons:
        key = repr(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def installation_order(plan: Dict[str, Candidate]) -> List[str]:
    """按依赖拓扑序给出安装顺序（被依赖者先装），平局按插件名。"""
    plugins = set(plan)
    indeg = {p: 0 for p in plugins}
    radj: Dict[str, List[str]] = {p: [] for p in plugins}
    for p, cand in plan.items():
        for dep, _ in cand.dependencies:
            if dep in plugins:
                indeg[p] += 1
                radj[dep].append(p)
    ready = sorted(p for p, d in indeg.items() if d == 0)
    order: List[str] = []
    while ready:
        p = ready.pop(0)
        order.append(p)
        for q in sorted(radj[p]):
            indeg[q] -= 1
            if indeg[q] == 0:
                ready.append(q)
                ready.sort()
    return order


def plan_diff(
    baseline_versions: Dict[str, Version],
    plan: Dict[str, Candidate],
) -> Dict[str, dict]:
    """逐插件对比原方案与新方案，动作分为 keep/change/add/remove。

    * keep：插件在两方案中均存在且版本相同；
    * change：同一插件存在于两方案但版本不同（换版）；
    * add：原方案没有、新方案新增；
    * remove：原方案有、新方案移除。
    """
    out: Dict[str, dict] = {}
    for name in sorted(plan):
        new_version = str(plan[name].version)
        old = baseline_versions.get(name)
        if old is None:
            out[name] = {"action": "add", "from_version": None,
                         "to_version": new_version}
        elif old == plan[name].version:
            out[name] = {"action": "keep", "from_version": str(old),
                         "to_version": new_version}
        else:
            out[name] = {"action": "change", "from_version": str(old),
                         "to_version": new_version}
    for name in sorted(baseline_versions):
        if name not in plan:
            out[name] = {"action": "remove", "from_version": str(baseline_versions[name]),
                         "to_version": None}
    return out


def change_count(diff: Dict[str, dict]) -> int:
    """新增、移除、换版的插件总数（keep 不计）。"""
    return sum(1 for d in diff.values() if d["action"] != "keep")
