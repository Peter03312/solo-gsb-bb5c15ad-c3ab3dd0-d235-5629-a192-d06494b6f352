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
import threading
import uuid

from .solver import Candidate, RuntimeRequirement, Snapshot
from .version import InvalidSpecifier, InvalidVersion, SpecifierSet, Version

__all__ = [
    "PublishError", "BatchConflict", "BatchStorageError", "RepairStorageError",
    "validate_metadata_specs", "verify_sha256",
    "save_package_atomic", "canonical_request", "canonical_batch",
    "sha256_of", "inspect_package_file", "repair_package_atomic",
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


class RepairStorageError(OSError):
    """修复写盘失败。调用方必须明确拒绝并保留原（缺失/损坏）文件。"""


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


# ---------------------------------------------------------------- 核验与修复

# 核验状态（对外用稳定英文标识）
STATUS_OK = "ok"          # 完好：文件存在且 SHA-256 与记录一致
STATUS_MISSING = "missing"  # 缺失：内容寻址文件不存在
STATUS_CORRUPT = "corrupt"  # 损坏：文件存在但 SHA-256 与记录不一致

# 修复结果
REPAIR_REPAIRED = "repaired"  # 本次完成了原子替换
REPAIR_NOT_NEEDED = "not_needed"  # 文件本就完好，未改写


def sha256_of(path: str) -> str:
    """流式计算文件 SHA-256（与发布复核同款分块读取）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_package_file(storage_path: str, expected_sha256: str
                         ) -> tuple[str, int | None, str | None]:
    """核验一个内容寻址包文件相对记录摘要的状态。

    返回 ``(status, size_bytes, actual_sha256)``：
    * ``ok``      文件存在且摘要一致（actual_sha256 即记录摘要）；
    * ``missing`` 路径不存在（size/actual 均为 None）；
    * ``corrupt``  文件存在但摘要不一致，actual_sha256 为实际摘要
                   （无法读取时记为空串，仍按损坏处理）。
    纯文件系统操作，不触碰任何版本元数据。
    """
    if not os.path.exists(storage_path):
        return STATUS_MISSING, None, None
    try:
        actual = sha256_of(storage_path)
    except OSError:
        return STATUS_CORRUPT, None, ""
    if actual != expected_sha256.lower():
        try:
            size: int | None = os.path.getsize(storage_path)
        except OSError:
            size = None
        return STATUS_CORRUPT, size, actual
    return STATUS_OK, os.path.getsize(storage_path), actual


def repair_package_atomic(src_path: str, expected_sha256: str,
                          storage_path: str) -> tuple[str, int]:
    """用已通过摘要复核的替换文件**原子修复**缺失/损坏的包。

    目标路径按 SHA-256 内容寻址。并发安全保证：

    * 目标完好（存在且摘要一致）→ 绝不改写，返回 ``not_needed``；
    * 目标缺失/损坏 → 先尝试硬链接占位（同文件系统上的原子 create）：
      链接成功即修复完成（替换文件本身保留），绝不产生部分写入内容；
      跨文件系统等不支持硬链接时，回退为同目录临时文件 + ``os.replace``
      原子重命名（替换文件被移动）；
    * 与发布/下载/另一修复并发时，任何已确认完好的文件都不会被覆盖：
      竞争落败后重读目标状态，完好即视为无需修复；
    * 任何写盘失败抛 :class:`RepairStorageError`，原文件保持原状
      （缺失仍缺失、损坏文件不被半截内容覆盖）。

    返回 ``(result, size_bytes)``。
    """
    digest = expected_sha256.lower()
    status, size, _actual = inspect_package_file(storage_path, digest)
    if status == STATUS_OK:
        return REPAIR_NOT_NEEDED, size

    # 1) 优先硬链接占位：link 是原子 create，天然不覆盖已有文件。
    #    成功时替换文件保留（同 inode），调用方仍可清理其临时副本。
    try:
        os.makedirs(os.path.dirname(storage_path), exist_ok=True)
        os.link(src_path, storage_path)
    except FileExistsError:
        # 并发修复/发布刚刚把文件放好：只认完好文件，绝不覆盖
        status, size, _actual = inspect_package_file(storage_path, digest)
        if status == STATUS_OK:
            return REPAIR_NOT_NEEDED, size
        # 目标仍是损坏文件：硬链接无法覆盖，回退到原子重命名
    except OSError:
        # EXDEV（跨文件系统）/EPERM 等：回退原子重命名
        pass
    else:
        _verify_repaired(storage_path, digest)
        return REPAIR_REPAIRED, os.path.getsize(storage_path)

    # 2) 回退：同目录临时文件 + os.replace 原子重命名。
    bucket = os.path.dirname(storage_path)
    tmp_path = os.path.join(
        bucket, f".repair-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}")
    try:
        os.makedirs(bucket, exist_ok=True)
        try:
            shutil.move(src_path, tmp_path)
        except OSError as e:
            raise RepairStorageError(f"替换文件写入临时位置失败: {e}") from e
        # 落盘后、替换前自检：暂存内容必须即记录摘要（纯函数自包含，
        # 不依赖调用方预先校验），不符则中止，原缺失/损坏文件不动。
        try:
            staged_digest = sha256_of(tmp_path)
        except OSError as e:
            raise RepairStorageError(f"替换文件复核失败: {e}") from e
        if staged_digest != digest:
            raise RepairStorageError(
                f"替换文件摘要不符（{staged_digest} != {digest}），拒绝替换")
        try:
            with open(tmp_path, "rb") as f:
                os.fsync(f.fileno())
        except OSError as e:
            raise RepairStorageError(f"替换文件刷盘失败: {e}") from e
        # 替换前最后确认：并发的另一修复/发布可能已把文件恢复完好。
        # 完好文件绝不覆盖（即使字节相同），清理暂存后按无需修复返回。
        status, size, _a = inspect_package_file(storage_path, digest)
        if status == STATUS_OK:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return REPAIR_NOT_NEEDED, size
        try:
            os.replace(tmp_path, storage_path)
        except OSError as e:
            raise RepairStorageError(f"原子替换失败: {e}") from e
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    _verify_repaired(storage_path, digest)
    return REPAIR_REPAIRED, os.path.getsize(storage_path)


def _verify_repaired(storage_path: str, digest: str) -> None:
    """替换后复核：落盘结果必须与记录摘要一致，否则视为写盘失败并保留现场。"""
    status, _size, _actual = inspect_package_file(storage_path, digest)
    if status != STATUS_OK:
        raise RepairStorageError("修复后复核未通过：落盘内容与记录摘要不一致")


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
