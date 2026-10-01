"""SQLAlchemy ORM 模型。

目录修订（catalog revision）
---------------------------
每次成功的发布/撤销都把 revision 计数器 +1，形成一个新的只读目录修订。
版本行通过 published_revision / revoked_reversion 记录生命周期：

* published_revision <= R 且 (revoked_revision 为空或 R < revoked_revision)
  => 版本在修订 R 中可见（未撤销）。
* 撤销后历史方案仍引用原版本行，查询时附带当前 revoked 标记。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# PostgreSQL 上使用原生 JSONB；其它方言（如测试用 SQLite）回退到通用 JSON
JSONB = JSON().with_variant(PG_JSONB(), "postgresql").with_variant(JSON(), "sqlite")

# SQLite 仅对声明类型恰为 INTEGER 的主键做 rowid 自增；生产 PostgreSQL
# 仍使用 BIGINT 主键。外键列保持 BigInteger（SQLite 对类型亲和不敏感）。
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CatalogRevision(Base):
    __tablename__ = "catalog_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # publish / batch / revoke / init
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    plugin: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class PublishBatch(Base):
    """一次离线批次交付：客户端提供的批次号 + 规范化内容哈希。

    * batch_id 为主键：相同批次号的并发提交在本行上串行化，只能形成一个
      目录修订（后到者读到先提交者的原结果）。
    * content_hash 与 batch_id 同时确定：同号必须同内容，否则 409。
    * revision_id 唯一：一个批次至多对应一个目录修订；批次内全部成员的
      published_revision 都等于该修订号，因而同时可见。
    """
    __tablename__ = "publish_batches"

    batch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("catalog_revisions.revision"),
        nullable=True, unique=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Plugin(Base):
    __tablename__ = "plugins"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    versions: Mapped[list["PluginVersion"]] = relationship(
        back_populates="plugin", order_by="PluginVersion.id")


class PluginVersion(Base):
    __tablename__ = "plugin_versions"
    __table_args__ = (
        UniqueConstraint("plugin_id", "version", name="uq_plugin_version"),
        Index("ix_pv_lifecycle", "published_revision", "revoked_revision"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    plugin_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plugins.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)

    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)

    published_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    revoked_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    plugin: Mapped[Plugin] = relationship(back_populates="versions")
    dependencies: Mapped[list["Dependency"]] = relationship(
        back_populates="source", cascade="all, delete-orphan")
    runtimes: Mapped[list["VersionRuntime"]] = relationship(
        back_populates="version", cascade="all, delete-orphan")
    conflicts: Mapped[list["Conflict"]] = relationship(
        back_populates="source", cascade="all, delete-orphan")


class Dependency(Base):
    """某版本声明的依赖：对 dep_plugin 的版本范围要求。"""
    __tablename__ = "dependencies"
    __table_args__ = (
        UniqueConstraint("source_id", "dep_plugin", name="uq_dependency"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plugin_versions.id", ondelete="CASCADE"), nullable=False)
    dep_plugin: Mapped[str] = mapped_column(String(128), nullable=False)
    spec: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    source: Mapped[PluginVersion] = relationship(back_populates="dependencies")


class VersionRuntime(Base):
    """某版本兼容的运行环境及该环境版本范围（空字符串=任意版本）。"""
    __tablename__ = "version_runtimes"
    __table_args__ = (
        UniqueConstraint("version_id", "name", name="uq_version_runtime"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plugin_versions.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    spec: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    version: Mapped[PluginVersion] = relationship(back_populates="runtimes")


class Conflict(Base):
    """某版本声明的互斥插件（与该插件的任意已选版本不得共存）。"""
    __tablename__ = "conflicts"
    __table_args__ = (
        UniqueConstraint("source_id", "conflict_plugin", name="uq_conflict"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plugin_versions.id", ondelete="CASCADE"), nullable=False)
    conflict_plugin: Mapped[str] = mapped_column(String(128), nullable=False)

    source: Mapped[PluginVersion] = relationship(back_populates="conflicts")


class Solution(Base):
    """一次成功求解的完整方案。失败不保存。"""
    __tablename__ = "solutions"
    __table_args__ = (
        # 同一目录修订 + 同一请求（规范化后）只会有一份方案
        UniqueConstraint("revision_id", "request_hash", name="uq_solution_request"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("catalog_revisions.revision"), nullable=False, index=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    selection: Mapped[dict] = mapped_column(JSONB, nullable=False)
    install_order: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
