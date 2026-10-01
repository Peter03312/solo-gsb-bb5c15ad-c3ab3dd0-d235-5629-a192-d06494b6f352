"""基于 SQLAlchemy 的目录修订持久化。

修订可见性
----------
每个版本记录 ``published_revision`` 与 ``revoked_revision``：
在修订 R 可见 ⇔ published_revision <= R 且
（未撤销 或 R < revoked_revision）。

ORM 相关导入延迟到函数内，使本模块可在没有 sqlalchemy 的环境下被加载
（测试可用内存实现替换这些函数，见 tests/test_catalog_flow.py）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterable

from . import catalog
from .solver import Candidate, RuntimeRequirement, Snapshot
from .version import SpecifierSet, Version


def _orm():
    """延迟加载 ORM 符号。"""
    from sqlalchemy import func, select
    from .models import (
        CatalogRevision, Conflict, Dependency, Plugin, PluginVersion,
        PublishBatch, Solution, VersionRuntime,
    )
    return SimpleNamespace(
        func=func, select=select, CatalogRevision=CatalogRevision,
        Conflict=Conflict, Dependency=Dependency, Plugin=Plugin,
        PluginVersion=PluginVersion, PublishBatch=PublishBatch,
        Solution=Solution, VersionRuntime=VersionRuntime,
    )


@dataclass
class BatchOutcome:
    """一次批次提交的结果（新提交或幂等命中）。"""
    batch_id: str
    revision: int
    cached: bool
    content_hash: str
    members: list  # [(plugin_name, version, pv_or_None)]
    created_paths: list[str]
    batch_row: object = None


# ---------------------------------------------------------------- 修订

def latest_revision(db: Any) -> int:
    o = _orm()
    return db.execute(
        o.select(o.func.coalesce(o.func.max(o.CatalogRevision.revision), 0))
    ).scalar_one()


def bump_revision(db: Any, *, kind: str, plugin: str | None,
                  version: str | None, note: str | None = None):
    o = _orm()
    row = o.CatalogRevision(kind=kind, plugin=plugin, version=version, note=note)
    db.add(row)
    db.flush()  # 取自增修订号；同事务内串行，保证单调
    return row


def _get_or_create_plugin(db: Any, name: str):
    o = _orm()
    plugin = db.execute(
        o.select(o.Plugin).where(o.Plugin.name == name)
    ).scalar_one_or_none()
    if plugin is None:
        plugin = o.Plugin(name=name)
        db.add(plugin)
        db.flush()
    return plugin


def _existing_version(db: Any, plugin, version: str):
    o = _orm()
    return db.execute(
        o.select(o.PluginVersion).where(
            o.PluginVersion.plugin_id == plugin.id,
            o.PluginVersion.version == version,
        )
    ).scalar_one_or_none()


def _insert_version(db: Any, *, plugin, metadata, sha256: str,
                    original_filename: str, storage_path: str, size: int,
                    revision_no: int):
    o = _orm()
    pv = o.PluginVersion(
        plugin_id=plugin.id,
        version=metadata.version,
        sha256=sha256,
        filename=os.path.basename(original_filename) or "package.bin",
        size_bytes=size,
        storage_path=storage_path,
        published_revision=revision_no,
    )
    db.add(pv)
    db.flush()

    for dep in metadata.dependencies:
        db.add(o.Dependency(source_id=pv.id, dep_plugin=dep.plugin,
                            spec=dep.spec or ""))
    for rt in metadata.runtimes:
        db.add(o.VersionRuntime(version_id=pv.id, name=rt.name,
                                spec=rt.spec or ""))
    for name in metadata.conflicts:
        db.add(o.Conflict(source_id=pv.id, conflict_plugin=name))
    return pv


def publish_version(db: Any, *, metadata, src_path: str,
                    original_filename: str, package_dir: str):
    """同事务：校验 -> 查重 -> 摘要校验 -> 落盘 -> 写行 -> 修订 +1。"""
    catalog.validate_metadata_specs(metadata)

    plugin = _get_or_create_plugin(db, metadata.plugin)
    existing = _existing_version(db, plugin, metadata.version)
    if existing is not None:
        raise catalog.PublishError(
            f"{metadata.plugin}=={metadata.version} 已发布（修订 "
            f"{existing.published_revision}），同名同版本不可改写；"
            "如已撤销请发布新版本号")

    catalog.verify_sha256(src_path, metadata.sha256)

    storage_path, size, _created = catalog.save_package_atomic(
        src_path, metadata.sha256, package_dir)

    revision = bump_revision(
        db, kind="publish", plugin=metadata.plugin, version=metadata.version)

    pv = _insert_version(
        db, plugin=plugin, metadata=metadata, sha256=metadata.sha256,
        original_filename=original_filename, storage_path=storage_path,
        size=size, revision_no=revision.revision)
    db.flush()
    return pv, revision.revision


# ---------------------------------------------------------------- 批次发布

def get_batch(db: Any, batch_id: str):
    """按批次号读取已提交批次；不存在返回 None。"""
    o = _orm()
    return db.get(o.PublishBatch, batch_id)


def _members_of_batch(db: Any, revision_no: int):
    """读取某批次修订下的全部成员版本行（按插件名、版本排序）。"""
    o = _orm()
    return list(db.execute(
        o.select(o.PluginVersion, o.Plugin.name)
        .join(o.Plugin, o.Plugin.id == o.PluginVersion.plugin_id)
        .where(o.PluginVersion.published_revision == revision_no)
        .order_by(o.Plugin.name, o.PluginVersion.version)
    ).all())


def _load_batch_outcome(db: Any, batch_row, *, cached: bool) -> BatchOutcome:
    rows = _members_of_batch(db, batch_row.revision_id)
    members = [(name, pv.version, pv) for pv, name in rows]
    return BatchOutcome(
        batch_id=batch_row.batch_id, revision=batch_row.revision_id,
        cached=cached, content_hash=batch_row.content_hash, members=members,
        created_paths=[], batch_row=batch_row)


def items_content_hash(items: list) -> str:
    metas = [m for m, _s, _f in items]
    hashes = [m.sha256 for m, _s, _f in items]
    h, _ = catalog.canonical_batch(metas, hashes)
    return h


def canonical_items(items: list):
    metas = [m for m, _s, _f in items]
    hashes = [m.sha256 for m, _s, _f in items]
    return catalog.canonical_batch(metas, hashes)


def publish_batch(db: Any, *, batch_id: str, items: list, package_dir: str,
                  created_paths_sink: list[str] | None = None):
    """单个事务内完成整批发布；调用方负责最终 commit / rollback。

    ``items`` 为 ``[(metadata, src_path, original_filename), ...]``，
    metadata 与安装包 SHA-256 已在路由层逐项校验通过。
    若给出 ``created_paths_sink``，每新建一个内容寻址文件就把路径追加进去，
    便于调用方在任意异常分支回滚后回收孤儿文件。

    语义：
    * 批次内 (插件, 版本) 重复 → BatchConflict（409），整批不可见；
    * 任一成员同名同版本已在目录中（含已撤销）→ BatchConflict（409）；
    * 相同 batch_id + 相同内容重试 → 返回 cached=True 的原结果；
    * 相同 batch_id + 不同内容 → BatchConflict（409）；
    * 全部通过 → 只产生一个 kind="batch" 的目录修订，全部成员同时可见。

    返回 :class:`BatchOutcome`（含本次新落盘文件路径，失败回滚后供
    路由层做孤儿文件回收）。
    """
    o = _orm()

    # 1) 幂等性：批次号是主键，天然串行化并发提交
    existing_batch = db.get(o.PublishBatch, batch_id)
    if existing_batch is not None:
        if existing_batch.content_hash != items_content_hash(items):
            raise catalog.BatchConflict(
                f"批次 {batch_id} 已提交但内容不同（原内容哈希 "
                f"{existing_batch.content_hash}）；相同批次号必须提交相同内容")
        return _load_batch_outcome(db, existing_batch, cached=True)

    # 2) 批次内 (插件, 版本) 不得重复
    seen: dict[tuple[str, str], int] = {}
    for idx, (metadata, _src, _fname) in enumerate(items):
        key = (metadata.plugin, metadata.version)
        if key in seen:
            raise catalog.BatchConflict(
                f"批次内插件名与版本组合重复：{metadata.plugin}=="
                f"{metadata.version}（第 {seen[key] + 1} 项与第 {idx + 1} 项）")
        seen[key] = idx

    # 3) 逐项沿用单版本发布规则：同名同版本（即使已撤销）不可改写
    plugins_cache: dict[str, object] = {}
    for metadata, _src, _fname in items:
        plugin = plugins_cache.get(metadata.plugin)
        if plugin is None:
            plugin = _get_or_create_plugin(db, metadata.plugin)
            plugins_cache[metadata.plugin] = plugin
        if _existing_version(db, plugin, metadata.version) is not None:
            raise catalog.BatchConflict(
                f"{metadata.plugin}=={metadata.version} 已发布，"
                "同名同版本不可改写；批次中不得包含已存在版本")

    # 4) 内容寻址落盘（SHA-256 已在路由层复核）。失败必须整批中止：
    #    回滚后由路由层删除 created_paths 中本次新建的文件。
    stored: list[tuple] = []
    created_paths: list[str] = []
    for idx, (metadata, src_path, filename) in enumerate(items):
        try:
            storage_path, size, created = catalog.save_package_atomic(
                src_path, metadata.sha256, package_dir)
        except OSError as e:
            raise catalog.BatchStorageError(
                f"第 {idx + 1} 项 {metadata.plugin}=={metadata.version} "
                f"安装包落盘失败: {e}",
                created_paths=created_paths) from e
        if created:
            created_paths.append(storage_path)
            if created_paths_sink is not None:
                created_paths_sink.append(storage_path)
        stored.append((metadata, storage_path, size, filename))

    # 5) 占住批次号（主键冲突由调用方映射为 409/幂等重读）
    content_hash, _payload = canonical_items(items)
    batch_row = o.PublishBatch(
        batch_id=batch_id, content_hash=content_hash, revision_id=None,
        member_count=len(items))
    db.add(batch_row)
    db.flush()

    # 6) 只产生一个目录修订，全部成员共享该修订号 => 同时可见
    revision = bump_revision(
        db, kind="batch", plugin=None, version=None, note=batch_id)

    for metadata, storage_path, size, filename in stored:
        plugin = plugins_cache[metadata.plugin]
        _insert_version(
            db, plugin=plugin, metadata=metadata, sha256=metadata.sha256,
            original_filename=filename, storage_path=storage_path,
            size=size, revision_no=revision.revision)

    batch_row.revision_id = revision.revision
    db.flush()

    rows = _members_of_batch(db, revision.revision)
    members = [(name, pv.version, pv) for pv, name in rows]
    return BatchOutcome(
        batch_id=batch_id, revision=revision.revision, cached=False,
        content_hash=content_hash, members=members,
        created_paths=created_paths, batch_row=batch_row)


def revoke_version(db: Any, *, plugin_name: str, version: str,
                   reason: str | None) -> int:
    o = _orm()
    pv = db.execute(
        o.select(o.PluginVersion)
        .join(o.Plugin, o.Plugin.id == o.PluginVersion.plugin_id)
        .where(o.Plugin.name == plugin_name, o.PluginVersion.version == version)
    ).scalar_one_or_none()
    if pv is None:
        raise catalog.PublishError(f"未找到 {plugin_name}=={version}")
    if pv.revoked_revision is not None:
        raise catalog.PublishError(
            f"{plugin_name}=={version} 已在修订 {pv.revoked_revision} 撤销")

    revision = bump_revision(
        db, kind="revoke", plugin=plugin_name, version=version, note=reason)
    pv.revoked_revision = revision.revision
    pv.revoke_reason = reason
    db.flush()
    return revision.revision


# ---------------------------------------------------------------- 快照

def load_snapshot(db: Any, revision: int) -> Snapshot:
    o = _orm()
    versions = list(db.execute(
        o.select(o.PluginVersion, o.Plugin.name)
        .join(o.Plugin, o.Plugin.id == o.PluginVersion.plugin_id)
        .where(
            o.PluginVersion.published_revision <= revision,
            (o.PluginVersion.revoked_revision.is_(None))
            | (o.PluginVersion.revoked_revision > revision),
        )
    ).all())

    ids = [pv.id for pv, _ in versions]
    rel = _load_relations(db, ids)

    grouped: dict[str, list[Candidate]] = {}
    for pv, plugin_name in versions:
        runtimes = tuple(
            RuntimeRequirement(name=r.name, spec=SpecifierSet.parse(r.spec))
            for r in rel["runtimes"].get(pv.id, [])
        )
        deps = tuple(
            (d.dep_plugin, SpecifierSet.parse(d.spec))
            for d in sorted(rel["deps"].get(pv.id, []),
                            key=lambda d: d.dep_plugin)
        )
        conflicts = tuple(
            sorted(c.conflict_plugin for c in rel["conflicts"].get(pv.id, [])))
        grouped.setdefault(plugin_name, []).append(Candidate(
            plugin=plugin_name,
            version=Version.parse(pv.version),
            runtimes=runtimes,
            dependencies=deps,
            conflicts=conflicts,
        ))

    return Snapshot(
        revision=revision,
        candidates_by_plugin={
            name: tuple(sorted(cs, key=lambda c: c.version, reverse=True))
            for name, cs in grouped.items()
        },
    )


def _load_relations(db: Any, ids: list[int]) -> dict:
    o = _orm()
    result = {"deps": {}, "runtimes": {}, "conflicts": {}}
    if not ids:
        return result
    for d in db.execute(
            o.select(o.Dependency).where(o.Dependency.source_id.in_(ids))).scalars():
        result["deps"].setdefault(d.source_id, []).append(d)
    for r in db.execute(
            o.select(o.VersionRuntime).where(
                o.VersionRuntime.version_id.in_(ids))).scalars():
        result["runtimes"].setdefault(r.version_id, []).append(r)
    for c in db.execute(
            o.select(o.Conflict).where(o.Conflict.source_id.in_(ids))).scalars():
        result["conflicts"].setdefault(c.source_id, []).append(c)
    return result


# ---------------------------------------------------------------- 方案

def get_solution(db: Any, revision: int, request_hash: str):
    o = _orm()
    return db.execute(
        o.select(o.Solution).where(
            o.Solution.revision_id == revision,
            o.Solution.request_hash == request_hash,
        )
    ).scalar_one_or_none()


def save_solution(db: Any, *, revision: int, request_hash: str,
                  request: dict, selection: dict, install_order: list[str]):
    o = _orm()
    sol = o.Solution(
        revision_id=revision,
        request_hash=request_hash,
        request=request,
        selection=selection,
        install_order=install_order,
    )
    db.add(sol)
    db.flush()
    return sol


def revoked_flags(db: Any, version_ids: Iterable[int],
                  as_of_revision: int | None = None) -> dict[int, bool]:
    """版本撤销标记。

    ``as_of_revision`` 为空：返回**当前**是否已撤销（历史方案查询用，
    已撤销版本会带 revoked=true）。给定修订号 R：返回该版本在修订 R
    时点是否已撤销（求解/预演选到的版本在 R 必可见，恒为 false）。
    """
    o = _orm()
    ids = list(set(version_ids))
    if not ids:
        return {}
    rows = db.execute(
        o.select(o.PluginVersion.id, o.PluginVersion.revoked_revision)
        .where(o.PluginVersion.id.in_(ids))
    ).all()
    if as_of_revision is None:
        return {row[0]: row[1] is not None for row in rows}
    return {row[0]: row[1] is not None and row[1] <= as_of_revision
            for row in rows}
