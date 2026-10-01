"""目录修订领域逻辑（不依赖任何 Web/ORM 框架，便于离线测试）。

包含：
* 发布元数据校验（版本号、范围、互斥、重复依赖）
* 安装包 SHA-256 校验与内容寻址原子落盘
* 请求规范化哈希（同请求同哈希）

ORM 持久化操作见 :mod:`app.repository`。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil

from .solver import Candidate, RuntimeRequirement, Snapshot
from .version import InvalidSpecifier, InvalidVersion, SpecifierSet, Version

__all__ = [
    "PublishError", "BatchConflict", "BatchStorageError",
    "validate_metadata_specs", "verify_sha256",
    "save_package_atomic", "canonical_request", "canonical_batch",
]


class PublishError(ValueError):
    """发布被拒绝（摘要不符、范围非法、重复版本等）。"""


class BatchConflict(PublishError):
    """批次冲突：同号不同内容，或批次内/与目录既有版本撞车（HTTP 409）。"""


class BatchStorageError(IOError):
    """批次包文件落盘失败（调用方须回滚事务并清理已落盘文件）。"""

    def __init__(self, message: str, created_paths: list[str] | None = None):
        super().__init__(message)
        self.created_paths = created_paths or []


# ---------------------------------------------------------------- 校验

def validate_metadata_specs(metadata) -> None:
    """发布前校验：版本号合法、所有范围可解析、互斥项不含自身、依赖不重复。"""
    try:
        Version.parse(metadata.version)
    except InvalidVersion as e:
        raise PublishError(str(e)) from e

    for dep in metadata.dependencies:
        try:
            SpecifierSet.parse(dep.spec or "")
        except InvalidSpecifier as e:
            raise PublishError(f"依赖 {dep.plugin} 的版本范围无效: {e}") from e

    for rt in metadata.runtimes:
        try:
            SpecifierSet.parse(rt.spec or "")
        except InvalidSpecifier as e:
            raise PublishError(f"运行环境 {rt.name} 的版本范围无效: {e}") from e

    dep_names = [d.plugin for d in metadata.dependencies]
    if len(set(dep_names)) != len(dep_names):
        raise PublishError("同一依赖插件只能声明一条版本范围")

    rt_names = [r.name for r in metadata.runtimes]
    if len(set(rt_names)) != len(rt_names):
        raise PublishError("同一运行环境只能声明一条版本范围")

    if metadata.plugin in metadata.conflicts:
        raise PublishError("插件不能与自身互斥")


def verify_sha256(path: str, expected: str) -> None:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected.lower():
        raise PublishError(
            f"安装包摘要不符：声明 {expected}，实际 {actual}，已拒绝写入")


def save_package_atomic(src_path: str, sha256: str, dest_dir: str,
                        ) -> tuple[str, int, bool]:
    """以 sha256 前两字符分桶做内容寻址，先写临时文件再原子重命名。

    返回 ``(最终路径, 字节数, 本次是否真正创建了文件)``。失败批次回滚时
    只需删除 created=True 的路径——已存在的内容寻址文件可能属于别的
    （已成功）发布，绝不能动。
    """
    os.makedirs(dest_dir, exist_ok=True)
    bucket = os.path.join(dest_dir, sha256[:2])
    os.makedirs(bucket, exist_ok=True)
    final_path = os.path.join(bucket, sha256)
    size = os.path.getsize(src_path)
    created = False
    if not os.path.exists(final_path):
        tmp_path = os.path.join(bucket, f".tmp-{os.getpid()}-{sha256}")
        try:
            shutil.move(src_path, tmp_path)
            os.replace(tmp_path, final_path)
            created = True
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    return final_path, size, created


# ---------------------------------------------------------------- 请求规范化

def canonical_request(runtimes, targets: dict[str, str]) -> tuple[str, dict]:
    """规范化请求并计算 SHA-256：同请求（忽略键顺序）得到相同哈希。"""
    payload = {
        "runtimes": sorted(
            ({"name": r.name, "spec": str(r.spec)} for r in runtimes),
            key=lambda x: x["name"],
        ),
        "targets": {k: (v or "") for k, v in sorted(targets.items())},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), payload


# ---------------------------------------------------------------- 批次规范化

def canonical_batch(metadatas, hashes) -> tuple[str, list[dict]]:
    """计算批次内容哈希。

    内容 = 每个成员的（插件、版本、SHA-256、规范化后的运行环境/依赖/
    互斥项），按 (插件, 版本) 排序后序列化——成员提交顺序不影响内容。
    调用方须先用 :func:`validate_metadata_specs` 完成逐项校验。
    """
    members = []
    for meta, sha in zip(metadatas, hashes):
        members.append({
            "plugin": meta.plugin,
            "version": meta.version,
            "sha256": sha,
            "runtimes": sorted(
                ({"name": r.name, "spec": r.spec or ""} for r in meta.runtimes),
                key=lambda x: x["name"]),
            "dependencies": sorted(
                ({"plugin": d.plugin, "spec": d.spec or ""}
                 for d in meta.dependencies),
                key=lambda x: x["plugin"]),
            "conflicts": sorted(meta.conflicts),
        })
    members.sort(key=lambda m: (m["plugin"], m["version"]))
    payload = {"members": members}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), members


def collect_batch_package_files(db, storage_paths: list[str]) -> set[str]:
    """在新事务里查 storage_path 仍被任何版本行引用的路径。

    失败批次回滚后调用：引用集合之外的路径都是本次失败尝试引入的孤儿，
    由调用方删除；已被别的成功发布引用的内容寻址文件保留。
    """
    from .models import PluginVersion
    if not storage_paths:
        return set()
    referenced: set[str] = set()
    batch = 500
    for i in range(0, len(storage_paths), batch):
        chunk = storage_paths[i:i + batch]
        rows = db.query(PluginVersion.storage_path).filter(
            PluginVersion.storage_path.in_(chunk)).all()
        referenced.update(r[0] for r in rows)
    return referenced


def cleanup_orphan_files(db, created_paths: list[str]) -> None:
    """失败批次回滚后的尽力垃圾回收：只删数据库中再无版本引用的文件。"""
    unique_paths = [p for p in dict.fromkeys(created_paths)]
    try:
        referenced = collect_batch_package_files(db, unique_paths)
    except Exception:
        return
    for path in unique_paths:
        if path in referenced:
            continue
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
