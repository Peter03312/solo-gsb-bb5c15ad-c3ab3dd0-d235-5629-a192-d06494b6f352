"""求解服务：从快照构造求解器输入、执行求解、持久化方案。"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import catalog, repository
from .models import Plugin, PluginVersion, Solution
from .solver import (
    Candidate, RuntimeRequirement, change_count, installation_order,
    plan_diff, solve as solver_run, solve_minimal_change,
)
from .version import InvalidSpecifier, SpecifierSet, Version


class RequestError(ValueError):
    """请求参数问题（400）。"""


class SolutionNotFound(LookupError):
    """历史方案不存在（404）。"""


@dataclass
class SolveOutcome:
    solution_row: object
    cached: bool
    selection: dict
    install_order: list[str]
    revision: int


@dataclass
class PreviewOutcome:
    """一次预演结果（不落库、不产生修订）。"""
    source_solution_id: int
    revision: int
    selection: dict
    install_order: list[str]
    changes: dict[str, dict]
    change_counts: dict[str, int]
    effective_runtimes: list[dict]
    effective_targets: dict[str, str]
    request_hash: str


def _parse_targets(targets: dict[str, str]) -> dict[str, SpecifierSet]:
    parsed: dict[str, SpecifierSet] = {}
    for name, spec in targets.items():
        try:
            parsed[name] = SpecifierSet.parse(spec or "")
        except InvalidSpecifier as e:
            raise RequestError(f"目标插件 {name} 的版本范围无效: {e}") from e
    return parsed


def _parse_runtimes(runtimes) -> list[RuntimeRequirement]:
    out: list[RuntimeRequirement] = []
    seen = set()
    for rt in runtimes:
        if rt.name in seen:
            raise RequestError(f"运行环境 {rt.name} 重复提交")
        seen.add(rt.name)
        try:
            out.append(RuntimeRequirement(name=rt.name,
                                          spec=SpecifierSet.parse(rt.spec or "")))
        except InvalidSpecifier as e:
            raise RequestError(f"运行环境 {rt.name} 的版本范围无效: {e}") from e
    return out


def resolve_revision(db: Session, requested_revision: int | None) -> int:
    if requested_revision is not None:
        from .models import CatalogRevision
        exists = db.execute(
            select(CatalogRevision.revision)
            .where(CatalogRevision.revision == requested_revision)
        ).scalar_one_or_none()
        if exists is None:
            raise RequestError(f"目录修订 {requested_revision} 不存在")
        return requested_revision
    return repository.latest_revision(db)


def _load_version_rows(db: Session, keys: list[tuple[str, str]]) -> dict:
    if not keys:
        return {}
    rows = db.execute(
        select(PluginVersion, Plugin.name)
        .join(Plugin, Plugin.id == PluginVersion.plugin_id)
        .where(
            Plugin.name.in_([k[0] for k in keys]),
            PluginVersion.version.in_([k[1] for k in keys]),
        )
    ).all()
    return {(name, pv.version): pv for pv, name in rows}


def _build_selection(db: Session, plan: dict[str, Candidate]) -> dict:
    """由求解器方案构造可序列化 selection（含 sha256、package_url、version_id）。"""
    keys = [(name, str(cand.version)) for name, cand in plan.items()]
    lookup = _load_version_rows(db, keys)

    selection: dict = {}
    for name in sorted(plan):
        cand = plan[name]
        pv = lookup[(name, str(cand.version))]
        selection[name] = {
            "plugin": name,
            "version": str(cand.version),
            "sha256": pv.sha256,
            "dependencies": [
                {"plugin": d, "spec": str(spec)} for d, spec in cand.dependencies
            ],
            "runtimes": [
                {"name": r.name, "spec": str(r.spec)} for r in cand.runtimes
            ],
            "conflicts": list(cand.conflicts),
            "package_url": f"/api/v1/plugins/{name}/versions/{str(cand.version)}/download",
            "version_id": pv.id,
        }
    return selection


def execute(db: Session, *, revision: int,
            runtimes_in, targets_in: dict[str, str]) -> SolveOutcome:
    runtime_reqs = _parse_runtimes(runtimes_in)
    target_specs = _parse_targets(targets_in)

    request_hash, canonical = catalog.canonical_request(runtime_reqs, targets_in)
    canonical_with_revision = {"revision": revision, **canonical}

    cached = repository.get_solution(db, revision, request_hash)
    if cached is not None:
        return SolveOutcome(
            solution_row=cached, cached=True,
            selection=cached.selection, install_order=cached.install_order,
            revision=revision,
        )

    snapshot = repository.load_snapshot(db, revision)
    plan = solver_run(snapshot, runtime_reqs, target_specs)
    order = installation_order(plan)
    selection = _build_selection(db, plan)

    sol = repository.save_solution(
        db,
        revision=revision,
        request_hash=request_hash,
        request=canonical_with_revision,
        selection=selection,
        install_order=order,
    )
    return SolveOutcome(solution_row=sol, cached=False, selection=selection,
                        install_order=order, revision=revision)


def _baseline_request(row: Solution) -> tuple[list, dict[str, str]]:
    """从历史方案保存的规范化请求中还原运行环境与目标（原样，含空 spec）。"""
    request = row.request or {}
    runtimes = [SimpleNamespace(name=r["name"], spec=r.get("spec", ""))
                for r in (request.get("runtimes") or [])]
    targets = dict(request.get("targets") or {})
    return runtimes, targets


def effective_request(
    row: Solution,
    *,
    runtimes_in,
    targets_in: dict[str, str] | None,
) -> tuple[list, dict[str, str], list[RuntimeRequirement], dict[str, SpecifierSet],
           list[dict]]:
    """合并原请求与覆盖：给定覆盖项整体替换对应部分，未给定则沿用原方案请求。

    第五项为运行环境回显，统一使用规范化 spec 文本（与 request_hash 的
    规范化负载一致，保证稳定可复现）。
    """
    base_runtimes, base_targets = _baseline_request(row)
    runtimes_raw = base_runtimes if runtimes_in is None else runtimes_in
    targets_raw = base_targets if targets_in is None else targets_in

    runtime_reqs = _parse_runtimes(runtimes_raw)
    target_specs = _parse_targets(targets_raw)

    runtimes_echo = [{"name": r.name, "spec": str(r.spec)}
                     for r in sorted(runtime_reqs, key=lambda r: r.name)]
    targets_plain = {
        name: str(spec) for name, spec in
        sorted(target_specs.items(), key=lambda kv: kv[0])
    }
    return runtimes_raw, targets_plain, runtime_reqs, target_specs, runtimes_echo


def preview(
    db: Session,
    *,
    solution_id: int,
    revision: int,
    runtimes_in,
    targets_in: dict[str, str] | None,
) -> PreviewOutcome:
    """对历史方案在目标修订上做不保存的预演（最小变动优先）。"""
    row = db.get(Solution, solution_id)
    if row is None:
        raise SolutionNotFound(f"方案 {solution_id} 不存在")

    (_, targets_plain, runtime_reqs, target_specs,
     runtimes_echo) = effective_request(
        row, runtimes_in=runtimes_in, targets_in=targets_in)

    request_hash, _ = catalog.canonical_request(runtime_reqs, targets_plain)

    # 原方案逐插件版本，作为“最小变动”的比较基线
    baseline_versions: dict[str, Version] = {}
    for name, item in (row.selection or {}).items():
        baseline_versions[name] = Version.parse(item["version"])

    snapshot = repository.load_snapshot(db, revision)
    plan = solve_minimal_change(
        snapshot, runtime_reqs, target_specs, baseline_versions)
    order = installation_order(plan)
    selection = _build_selection(db, plan)
    diff = plan_diff(baseline_versions, plan)

    counts = {action: 0 for action in ("keep", "change", "add", "remove")}
    for d in diff.values():
        counts[d["action"]] += 1

    return PreviewOutcome(
        source_solution_id=solution_id,
        revision=revision,
        selection=selection,
        install_order=order,
        changes=diff,
        change_counts={**counts, "changed": change_count(diff)},
        effective_runtimes=runtimes_echo,
        effective_targets=targets_plain,
        request_hash=request_hash,
    )


def annotate_revoked(db: Session, selection: dict,
                     as_of_revision: int | None = None) -> dict:
    """给方案中的每个选中项补上撤销标记。

    历史查询（``as_of_revision=None``）反映**当前**撤销状态；
    求解/预演在修订 R 上选到的版本在 R 时点必可见，传 R 后恒为未撤销。
    """
    flags = repository.revoked_flags(
        db, [v["version_id"] for v in selection.values()],
        as_of_revision=as_of_revision)
    out = {}
    for name, item in selection.items():
        item = dict(item)
        item["revoked"] = flags.get(item["version_id"], False)
        item.pop("version_id", None)
        out[name] = item
    return out
