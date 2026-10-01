"""目录修订领域逻辑（不依赖任何 Web/ORM 框架，便于离线测试）。

包含：
* 发布元数据校验（版本号、范围、互斥、重复依赖）
* 安装包 SHA-256 校验、内容寻址原子落盘与原子修复替换
* 已发布安装包的文件状态核验（完好/缺失/损坏）
* 请求规范化哈希（同请求同哈希）

ORM 持久化操作见 :mod:`app.repository`。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import uuid

from .solver import Candidate, RuntimeRequirement, Snapshot
from .version import InvalidSpecifier, InvalidVersion, SpecifierSet, Version

__all__ = [
    "PublishError", "BatchConflict", "BatchStorageError", "RepairStorageError",
    "InstallResult",
    "validate_metadata_specs", "verify_sha256", "sha256_file",
    "inspect_package", "save_package_atomic", "install_package_atomic",
    "canonical_request", "canonical_batch",
]

# 文件状态：完好 / 缺失 / 损坏（内容与记录摘要不符）
STATE_OK = "intact"
STATE_MISSING = "missing"
STATE_CORRUPT = "corrupt"


class PublishError(ValueError):
    """发布被拒绝（摘要不符、范围非法、重复版本等）。"""


class BatchConflict(PublishError):
    """批次冲突：同号不同内容，或批次内/与目录既有版本撞车（HTTP 409）。"""


class BatchStorageError(IOError):
    """批次包文件落盘失败（调用方须回滚事务并清理已落盘文件）。"""

    def __init__(self, message: str, created_paths: list[str] | None = None):
        super().__init__(message)
        self.created_paths = created_paths or []


class RepairStorageError(IOError):
    """修复替换落盘失败：目标原文件（若有）保持不变。"""

    def __init__(self, message: str):
        super().__init__(message)


class InstallResult:
    """内容寻址安装/修复结果。

    * ``state`` 为安装后目标文件状态（始终为 ``intact``，损坏时若选择
      ``replace_corrupt=False`` 则不会进入安装流程，调用方先核验即可）；
    * ``pre_state`` 为安装前状态：``missing`` / ``intact`` / ``corrupt``；
    * ``created`` 表示是否新建了文件（供失败批次孤儿回收沿用）；
    * ``repaired`` 表示是否实际发生了替换（缺失补回或损坏修复）。
    """

    def __init__(self, *, path: str, size: int, pre_state: str,
                 created: bool, repaired: bool):
        self.path = path
        self.size = size
        self.pre_state = pre_state
        self.created = created
        self.repaired = repaired

    @property
    def state(self) -> str:
        return STATE_OK


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


def sha256_file(path: str) -> str:
    """流式计算文件 SHA-256。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path: str, expected: str) -> None:
    actual = sha256_file(path)
    if actual != expected.lower():
        raise PublishError(
            f"安装包摘要不符：声明 {expected}，实际 {actual}，已拒绝写入")


def inspect_package(path: str, expected_sha256: str) -> dict:
    """核验已记录安装包文件的当前状态（只读，不触碰文件）。

    返回 ``{"state": intact|missing|corrupt, "actual_sha256": str|None,
    "size_bytes": int}``：

    * ``missing``：路径不存在（长期保存介质丢失/卷未挂载）；
    * ``corrupt``：文件存在但 SHA-256 与记录不符（含被部分写入的文件）；
    * ``intact``：文件存在且 SHA-256 与记录一致。
    """
    expected_sha256 = expected_sha256.lower()
    if not os.path.exists(path):
        return {"state": STATE_MISSING, "actual_sha256": None, "size_bytes": 0}
    actual = sha256_file(path)
    if actual != expected_sha256:
        return {"state": STATE_CORRUPT, "actual_sha256": actual,
                "size_bytes": os.path.getsize(path)}
    return {"state": STATE_OK, "actual_sha256": actual,
            "size_bytes": os.path.getsize(path)}


def content_addressed_path(sha256: str, dest_dir: str) -> str:
    """摘要前两字符分桶的内容寻址最终路径。"""
    return os.path.join(dest_dir, sha256[:2], sha256)


@contextlib.contextmanager
def _digest_lock(sha256: str, dest_dir: str):
    """对同一摘要（即同一最终路径）的进程间排他锁。

    修复与发布、修复与修复若同时操作同一内容寻址文件，在此串行化，
    避免并发替换互相覆盖或读到对方的中间临时文件。锁文件置于
    ``dest_dir/.locks/`` 隐藏目录，不会被存储桶遍历误认成安装包。
    """
    import fcntl  # 本服务运行于 Linux（Docker/直跑均为 POSIX 环境）

    lock_dir = os.path.join(dest_dir, ".locks")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, f".{sha256}.lock")
    with open(lock_path, "a+") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _fsync_dir(path: str) -> None:
    """目录 fsync：保证重命名结果落盘（尽力而为，个别文件系统不支持）。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _verified_copy(src_path: str, dst_tmp: str, sha256: str) -> int:
    """流式复制到临时文件并即时复算摘要；不符/写失败不留半成品。

    返回字节数。调用方已在摘要锁内，临时文件名含 uuid 保证并发不互撞。
    """
    h = hashlib.sha256()
    size = 0
    fd = os.open(dst_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb", closefd=True) as out, open(src_path, "rb") as src:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dst_tmp)
        raise
    if h.hexdigest() != sha256.lower():
        with contextlib.suppress(OSError):
            os.unlink(dst_tmp)
        raise PublishError(
            f"安装包摘要不符：声明 {sha256}，实际 {h.hexdigest()}，已拒绝写入")
    return size


def install_package_atomic(src_path: str, sha256: str, dest_dir: str,
                           *, replace_corrupt: bool = True,
                           final_path: str | None = None) -> InstallResult:
    """在摘要排他锁内把已校验安装包原子安装到内容寻址路径。

    流程（对修复与发布统一）：

    1. 对同一 SHA-256 加进程间排他锁——修复与发布、修复与修复并发时
       在此串行；
    2. 复检目标当前状态：``intact`` 直接返回（**完好包不改写**）；
       ``corrupt`` 仅在 ``replace_corrupt=True`` 时替换，否则保留并返回；
       ``missing`` 补回；
    3. 先复制到同目录唯一临时文件、复算摘要并 fsync，再 ``os.replace``
       原子切换——任何下载请求只可能看到旧文件或完整新文件，绝不会
       读到部分写入；
    4. 写盘任一步失败：临时文件清理、原文件保留、向上抛出
       :class:`RepairStorageError`。

    ``final_path`` 可显式指定目标（修复以版本行记录的 storage_path 为准），
    默认按内容寻址规则 ``dest_dir/<前两位>/<sha256>``。
    """
    sha256 = sha256.lower()
    target = final_path or content_addressed_path(sha256, dest_dir)
    bucket = os.path.dirname(target)
    os.makedirs(bucket, exist_ok=True)

    with _digest_lock(sha256, dest_dir):
        before = inspect_package(target, sha256)
        pre_state = before["state"]
        if pre_state == STATE_OK:
            return InstallResult(
                path=target, size=before["size_bytes"], pre_state=pre_state,
                created=False, repaired=False)
        if pre_state == STATE_CORRUPT and not replace_corrupt:
            return InstallResult(
                path=target, size=before["size_bytes"], pre_state=pre_state,
                created=False, repaired=False)

        tmp_path = os.path.join(bucket, f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            size = _verified_copy(src_path, tmp_path, sha256)
            try:
                os.replace(tmp_path, target)
                _fsync_dir(bucket)
            except OSError as e:
                # replace 失败：tmp 清理，原文件（存在/损坏）原样保留
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise RepairStorageError(
                    f"安装包原子替换失败（原文件已保留）: {e}") from e
        except RepairStorageError:
            raise
        except PublishError:
            raise
        except OSError as e:
            raise RepairStorageError(
                f"安装包写盘失败（原文件已保留）: {e}") from e

        return InstallResult(
            path=target, size=size, pre_state=pre_state,
            created=(pre_state == STATE_MISSING),
            repaired=True)


def save_package_atomic(src_path: str, sha256: str, dest_dir: str,
                        ) -> tuple[str, int, bool]:
    """发布/批次使用的兼容封装：内容寻址原子落盘。

    返回 ``(最终路径, 字节数, 本次是否真正创建了文件)``。若既有的内容
    寻址文件已损坏（例如落盘后磁盘老化），发布同一摘要时会用新上传的
    完好文件**原子修复**它；完好文件永不改写。失败批次回滚时只需删除
    created=True 的路径——已存在且被其它成功发布引用的文件绝不能动。
    """
    result = install_package_atomic(src_path, sha256, dest_dir)
    return result.path, result.size, result.created


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
