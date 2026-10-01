"""Pydantic v2 请求/响应模型。"""
from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

PLUGIN_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")


def validate_plugin_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 128 or not PLUGIN_NAME_RE.match(name):
        raise ValueError(
            "插件名只允许小写字母、数字、. _ -，且首尾必须是字母或数字（最长 128）")
    return name


# ----------------------------------------------------------- 发布

class DependencyIn(BaseModel):
    plugin: str
    spec: str = ""

    @field_validator("plugin")
    @classmethod
    def _plugin(cls, v: str) -> str:
        return validate_plugin_name(v)


class RuntimeIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    spec: str = ""


class PublishMetadata(BaseModel):
    """发布接口 JSON 元数据（安装包通过 multipart 文件提交）。"""
    plugin: str
    version: str
    runtimes: list[RuntimeIn] = Field(default_factory=list)
    dependencies: list[DependencyIn] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("plugin")
    @classmethod
    def _plugin(cls, v: str) -> str:
        return validate_plugin_name(v)

    @field_validator("sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError("sha256 必须是 64 位十六进制")
        return v

    @field_validator("conflicts")
    @classmethod
    def _conflicts(cls, v: list[str]) -> list[str]:
        out = []
        for n in v:
            n = validate_plugin_name(n)
            if n not in out:
                out.append(n)
        return sorted(out)


class VersionOut(BaseModel):
    plugin: str
    version: str
    sha256: str
    filename: str
    size_bytes: int
    runtimes: list[RuntimeIn]
    dependencies: list[DependencyIn]
    conflicts: list[str]
    published_revision: int
    revoked_revision: int | None = None
    revoke_reason: str | None = None
    revoked: bool = False
    created_at: datetime


# ----------------------------------------------------------- 求解

class SolveRequest(BaseModel):
    # 不传 revision 表示使用当前最新目录修订
    revision: int | None = None
    runtimes: list[RuntimeIn] = Field(default_factory=list)
    # 同时接受 {"name": "spec"} 或 [{"plugin": ..., "spec": ...}]
    targets: dict[str, str] | list[DependencyIn]

    @field_validator("targets")
    @classmethod
    def _targets(cls, v):
        if isinstance(v, dict):
            if not v:
                raise ValueError("targets 不能为空")
            return {validate_plugin_name(k): str(spec or "") for k, spec in v.items()}
        if not v:
            raise ValueError("targets 不能为空")
        return {validate_plugin_name(d.plugin): d.spec for d in v}


class SelectedItem(BaseModel):
    plugin: str
    version: str
    sha256: str
    dependencies: list[DependencyIn]
    runtimes: list[RuntimeIn]
    conflicts: list[str]
    package_url: str
    revoked: bool = False


class SolutionOut(BaseModel):
    solution_id: int
    revision: int
    cached: bool
    request_hash: str
    selection: dict[str, SelectedItem]
    install_order: list[str]
    created_at: datetime


class ConflictDetail(BaseModel):
    detail: str
    code: str
    revision: int
    request_hash: str
    conflicts: list[dict]


# ----------------------------------------------------------- 方案预演（dry-run）

class PreviewRequest(BaseModel):
    """在目标修订上对已保存方案做不保存的预演。

    目标修订号必填；runtimes / targets 任一字段给出即用完整列表覆盖原请求
    的对应部分，省略则沿用原方案保存时的请求项。
    """
    revision: int = Field(..., description="目标目录修订号（必填）")
    runtimes: list[RuntimeIn] | None = None
    # 与 SolveRequest 相同，同时接受 {"name": "spec"} 或
    # [{"plugin": ..., "spec": ...}]；None 表示沿用原方案目标
    targets: dict[str, str] | list[DependencyIn] | None = None

    @field_validator("targets")
    @classmethod
    def _targets(cls, v):
        if v is None:
            return None
        if isinstance(v, dict):
            if not v:
                raise ValueError("targets 不能为空（如需沿用原请求请省略该字段）")
            return {validate_plugin_name(k): str(spec or "") for k, spec in v.items()}
        if not v:
            raise ValueError("targets 不能为空（如需沿用原请求请省略该字段）")
        return {validate_plugin_name(d.plugin): d.spec for d in v}


class PluginChange(BaseModel):
    action: str  # keep / change / add / remove
    from_version: str | None = None
    to_version: str | None = None


class ChangeCounts(BaseModel):
    keep: int = 0
    change: int = 0
    add: int = 0
    remove: int = 0
    changed: int = 0  # = change + add + remove


class PreviewOut(BaseModel):
    """预演结果：不保存、不产生方案 ID；差异逐插件给出。"""
    source_solution_id: int
    revision: int
    saved: bool = False
    request_hash: str
    runtimes: list[RuntimeIn]
    targets: dict[str, str]
    selection: dict[str, SelectedItem]
    install_order: list[str]
    change_counts: ChangeCounts
    changes: dict[str, PluginChange]


# ----------------------------------------------------------- 通用

class RevisionOut(BaseModel):
    revision: int
    created_at: datetime
    kind: str
    plugin: str | None
    version: str | None
    note: str | None


class RevokeIn(BaseModel):
    reason: str | None = None


# ----------------------------------------------------------- 批次发布

BATCH_ID_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")


def validate_batch_id(batch_id: str) -> str:
    batch_id = (batch_id or "").strip()
    if not batch_id or len(batch_id) > 128 or not BATCH_ID_RE.match(batch_id):
        raise ValueError(
            "批次标识只允许字母、数字、. _ -，且首尾必须是字母或数字（最长 128）")
    return batch_id


class BatchMemberOut(BaseModel):
    """批次中一个成员的发布结果（与单版本发布 VersionOut 同字段，外加下载地址）。"""
    index: int
    plugin: str
    version: str
    sha256: str
    filename: str
    size_bytes: int
    runtimes: list[RuntimeIn]
    dependencies: list[DependencyIn]
    conflicts: list[str]
    package_url: str
    published_revision: int
    revoked_revision: int | None = None
    revoke_reason: str | None = None
    revoked: bool = False


class BatchOut(BaseModel):
    """一次批次交付的完整结果。"""
    batch_id: str
    revision: int
    cached: bool
    content_hash: str
    member_count: int
    members: list[BatchMemberOut]
    created_at: datetime


class OkOut(BaseModel):
    ok: bool = True
    message: str
