"""HTTP 路由。

事务隔离
--------
发布/撤销使用默认 READ COMMITTED 事务并立即提交，提交后产生新目录修订。
求解使用 REPEATABLE READ 快照：一次方案从头到尾只读同一目录修订，
并发发布/撤销不会让方案混用不同修订；并发事务插入冲突方案时由
(uq_solution_request) 去重并重读。
"""
from __future__ import annotations

import json
import os
import tempfile

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from . import catalog, repository, service
from .config import settings
from .database import get_db
from .models import CatalogRevision, Plugin, PluginVersion, PublishBatch, Solution
from .schemas import (
    BatchOut, PreviewOut, PreviewRequest, PublishMetadata, RevisionOut,
    RevokeIn, SolutionOut, SolveRequest, VersionOut, validate_batch_id,
)
from .service import RequestError, SolutionNotFound, annotate_revoked
from .solver import CycleError, UnsatisfiableError
from .version import InvalidSpecifier

router = APIRouter()


def _safe_request_hash(body: SolveRequest) -> str:
    """对原始（已通过 pydantic 校验）的请求计算规范化哈希，供错误回显。"""
    targets = (body.targets if isinstance(body.targets, dict)
               else {d.plugin: d.spec for d in body.targets})
    try:
        runtime_reqs = service._parse_runtimes(body.runtimes)
        h, _ = catalog.canonical_request(runtime_reqs, targets)
        return h
    except RequestError:
        return ""


# ------------------------------------------------------------ 鉴权（可选）

def _check_token(authorization: str | None) -> None:
    token = settings.api_token
    if not token:
        return
    got = ""
    if authorization and authorization.startswith("Bearer "):
        got = authorization[len("Bearer "):].strip()
    if got != token:
        raise HTTPException(status_code=401, detail="缺少或无效的 API Token")


# ------------------------------------------------------------ 发布

@router.post("/plugins/{plugin_name}/versions", response_model=VersionOut,
             status_code=201, tags=["publish"])
async def publish(
    plugin_name: str,
    metadata: str = Form(..., description="发布元数据 JSON 字符串"),
    package: UploadFile = File(..., description="插件安装包"),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    _check_token(authorization)
    try:
        data = json.loads(metadata)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="metadata 不是合法 JSON")
    try:
        meta = PublishMetadata(**data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=_pydantic_errors(e))
    if meta.plugin != plugin_name:
        raise HTTPException(
            status_code=400,
            detail=f"路径插件名 {plugin_name} 与元数据 {meta.plugin} 不一致")

    # 流式落到临时文件，限制大小
    fd, tmp_path = tempfile.mkstemp(prefix="upload-", suffix=".pkg")
    too_large = False
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await package.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    too_large = True
                    break
                out.write(chunk)
        if too_large:
            raise HTTPException(
                status_code=413,
                detail=f"安装包超过上限 {settings.max_upload_bytes} 字节")

        try:
            pv, _revision_no = repository.publish_version(
                db, metadata=meta, src_path=tmp_path,
                original_filename=package.filename or "package.bin",
                package_dir=settings.package_dir,
            )
            db.commit()
        except catalog.PublishError as e:
            db.rollback()
            # 重复版本 409；摘要/范围/互斥自身等 422
            status = 409 if ("不可改写" in str(e) or "已发布" in str(e)) else 422
            raise HTTPException(status_code=status, detail=str(e))
        except IntegrityError:
            db.rollback()
            # 极端并发下的唯一约束竞争
            raise HTTPException(status_code=409, detail="同名同版本已存在（并发冲突）")
    finally:
        await package.close()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    db.refresh(pv)
    return _version_out(db, pv)


# ------------------------------------------------------------ 撤销

@router.post("/plugins/{plugin_name}/versions/{version}/revoke",
             response_model=VersionOut, tags=["publish"])
def revoke(plugin_name: str, version: str, body: RevokeIn | None = None,
           db: Session = Depends(get_db),
           authorization: str | None = Header(default=None)):
    _check_token(authorization)
    try:
        repository.revoke_version(
            db, plugin_name=plugin_name, version=version,
            reason=(body.reason if body else None))
        db.commit()
    except catalog.PublishError as e:
        db.rollback()
        status = 409 if ("已" in str(e)) else 404
        raise HTTPException(status_code=status, detail=str(e))

    pv = db.execute(
        select(PluginVersion).join(Plugin, Plugin.id == PluginVersion.plugin_id)
        .where(Plugin.name == plugin_name, PluginVersion.version == version)
    ).scalar_one()
    return _version_out(db, pv)


# ------------------------------------------------------------ 批次发布

async def _stream_upload(upload: UploadFile, tmp_path: str) -> int:
    """流式落盘单个安装包并校验大小上限，返回字节数。"""
    size = 0
    with open(tmp_path, "wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"安装包超过上限 {settings.max_upload_bytes} 字节")
            out.write(chunk)
    return size


def _parse_batch_member_metadata(raw: str, index: int) -> PublishMetadata:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"第 {index} 项 metadata 不是合法 JSON: {e}")
    try:
        return PublishMetadata(**data)
    except ValueError as e:
        errors = _pydantic_errors(e)
        raise HTTPException(
            status_code=422,
            detail={"message": f"第 {index} 项元数据校验失败",
                    "index": index, "errors": errors})


def _try_existing_batch_response(db: Session, batch_id: str, items: list,
                                 status_code: int = 200):
    """并发落败后重读赢家批次：同内容 -> 原结果；不同内容/不存在 -> None。"""
    existing = repository.get_batch(db, batch_id)
    if existing is None or existing.revision_id is None:
        return None
    if existing.content_hash != repository.items_content_hash(items):
        return False  # 同号不同内容：交由上层报 409
    outcome = repository._load_batch_outcome(db, existing, cached=True)
    return _batch_response(db, outcome, status_code=status_code)


@router.post("/batches", response_model=BatchOut, tags=["publish"],
             responses={201: {"model": BatchOut,
                              "description": "新批次提交成功（新目录修订）"},
                        200: {"model": BatchOut,
                              "description": "相同批次标识与内容重试，返回原结果"}})
async def publish_batch(
    batch_id: str = Form(..., description="客户端批次标识（同号重试必须同内容）"),
    metadata: list[str] = Form(
        ..., description="每份发布元数据 JSON 字符串，按序与 package 配对"),
    package: list[UploadFile] = File(..., description="各成员安装包，顺序与 metadata 对齐"),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    """一次离线批次交付：全部成员校验通过才生成**一个**目录修订。

    * 批次内 (插件名, 版本) 不得重复；逐项沿用单版本发布的名称/范围/
      SHA-256/同名同版本不可改写规则；
    * 任一项无效或冲突、或存储/提交失败 → 整批不可见（422/409/5xx），
      且不遗留仅由失败批次引入的包文件；
    * 相同批次标识 + 相同内容重试 → 返回原结果（``cached=true``，200）；
    * 相同批次标识 + 不同内容 → 409；并发同号重试只会形成一个修订。
    """
    _check_token(authorization)

    try:
        batch_id = validate_batch_id(batch_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not metadata:
        raise HTTPException(status_code=400, detail="批次至少包含一个成员")
    if len(metadata) != len(package):
        raise HTTPException(
            status_code=400,
            detail=f"metadata 份数（{len(metadata)}）与 package 份数"
                   f"（{len(package)}）不一致；二者必须按序一一配对")

    # 1) 逐项解析并校验元数据（版本号、范围、运行环境重复、互斥自身等）
    metas: list[PublishMetadata] = []
    for i, raw in enumerate(metadata, start=1):
        meta = _parse_batch_member_metadata(raw, i)
        try:
            catalog.validate_metadata_specs(meta)
        except catalog.PublishError as e:
            raise HTTPException(
                status_code=422,
                detail={"message": f"第 {i} 项 {meta.plugin}=={meta.version} 校验失败",
                        "index": i, "error": str(e)})
        metas.append(meta)

    # 2) 批次内 (插件, 版本) 不得重复（提前给 409，避免无谓落盘）
    seen: dict[tuple[str, str], int] = {}
    for i, meta in enumerate(metas, start=1):
        key = (meta.plugin, meta.version)
        if key in seen:
            raise HTTPException(
                status_code=409,
                detail={"message": "批次内插件名与版本组合重复",
                        "plugin": meta.plugin, "version": meta.version,
                        "indexes": [seen[key], i]})
        seen[key] = i

    # 3) 流式落临时文件，逐份复核 SHA-256（沿用单版本发布规则）
    tmp_paths: list[str] = []
    try:
        items = []
        for i, (meta, upload) in enumerate(zip(metas, package), start=1):
            fd, tmp_path = tempfile.mkstemp(prefix=f"batch-{i}-", suffix=".pkg")
            os.close(fd)
            tmp_paths.append(tmp_path)
            try:
                await _stream_upload(upload, tmp_path)
            except HTTPException:
                raise
            try:
                catalog.verify_sha256(tmp_path, meta.sha256)
            except catalog.PublishError as e:
                raise HTTPException(
                    status_code=422,
                    detail={"message": f"第 {i} 项 {meta.plugin}=={meta.version} 安装包校验失败",
                            "index": i, "error": str(e)})
            items.append((meta, tmp_path, upload.filename or "package.bin"))

        # 4) 单事务发布整批；失败回滚并回收本次新建的包文件
        created_paths: list[str] = []
        try:
            outcome = repository.publish_batch(
                db, batch_id=batch_id, items=items,
                package_dir=settings.package_dir,
                created_paths_sink=created_paths)
        except catalog.BatchConflict as e:
            db.rollback()
            raise HTTPException(status_code=409, detail={
                "detail": str(e), "code": "batch_conflict",
                "batch_id": batch_id})
        except catalog.BatchStorageError as e:
            db.rollback()
            _garbage_collect(db, list(e.created_paths))
            raise HTTPException(status_code=507, detail={
                "detail": str(e), "code": "batch_storage_failed",
                "batch_id": batch_id})
        except (IntegrityError, OperationalError):
            # 并发同号提交（唯一约束冲突或锁等待/序列化失败）：赢家可能已提交，
            # 回滚到干净事务后重读其原结果
            db.rollback()
            response = _try_existing_batch_response(db, batch_id, items)
            if response is False:
                raise HTTPException(status_code=409, detail={
                    "detail": f"批次 {batch_id} 已提交但内容不同",
                    "code": "batch_conflict", "batch_id": batch_id})
            if response is not None:
                return response
            # 赢家尚未提交/已回滚且本次是锁失败：无副作用地再试一轮
            try:
                outcome = repository.publish_batch(
                    db, batch_id=batch_id, items=items,
                    package_dir=settings.package_dir,
                    created_paths_sink=created_paths)
            except catalog.BatchConflict:
                db.rollback()
                raise HTTPException(status_code=409, detail={
                    "detail": f"批次 {batch_id} 并发提交冲突",
                    "code": "batch_conflict", "batch_id": batch_id})
            except (IntegrityError, OperationalError):
                db.rollback()
                response = _try_existing_batch_response(db, batch_id, items)
                if response is not None and response is not False:
                    return response
                _garbage_collect(db, created_paths)
                raise HTTPException(status_code=409, detail={
                    "detail": f"批次 {batch_id} 并发提交冲突，请稍后重试",
                    "code": "batch_conflict", "batch_id": batch_id})
        except Exception as exc:  # 其它存储/数据库意外失败：整批不可见
            db.rollback()
            _garbage_collect(db, created_paths)
            raise HTTPException(status_code=500, detail={
                "detail": f"批次提交失败，已全部回滚: {exc}",
                "code": "batch_failed", "batch_id": batch_id})

        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            # 提交失败：整批不可见，删除仅由本次尝试引入的包文件
            _garbage_collect(db, created_paths)
            # 并发下对端可能已先行提交同号同内容 -> 返回原结果
            response = _try_existing_batch_response(db, batch_id, items)
            if response is not None and response is not False:
                return response
            if response is False:
                raise HTTPException(status_code=409, detail={
                    "detail": f"批次 {batch_id} 已提交但内容不同",
                    "code": "batch_conflict", "batch_id": batch_id})
            raise HTTPException(status_code=500,
                                detail=f"批次提交失败，已全部回滚: {exc}")
    finally:
        for upload in package:
            await upload.close()
        for p in tmp_paths:
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    return _batch_response(db, outcome, status_code=200 if outcome.cached else 201)


@router.get("/batches/{batch_id}", response_model=BatchOut, tags=["publish"])
def get_batch(batch_id: str, db: Session = Depends(get_db)):
    """按批次标识查询原结果（只读，始终开放）。"""
    row = repository.get_batch(db, batch_id)
    if row is None or row.revision_id is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在或尚未提交成功")
    outcome = repository._load_batch_outcome(db, row, cached=True)
    return _batch_response(db, outcome, status_code=200)


def _garbage_collect(db: Session, created_paths: list[str]) -> None:
    """回滚后回收孤儿包文件：只删数据库中再无任何版本引用的内容寻址文件。"""
    if not created_paths:
        return
    try:
        catalog.cleanup_orphan_files(db, created_paths)
    except Exception:
        # 垃圾回收是尽力而为：事务失败语义（整批不可见、无元数据残留）已保证
        pass


def _batch_response(db: Session, outcome, *, status_code: int):
    from fastapi.responses import JSONResponse
    members = []
    for idx, (name, version, pv) in enumerate(outcome.members, start=1):
        members.append({
            "index": idx,
            "plugin": name,
            "version": version,
            "sha256": pv.sha256,
            "filename": pv.filename,
            "size_bytes": pv.size_bytes,
            "runtimes": [{"name": r.name, "spec": r.spec} for r in
                         sorted(pv.runtimes, key=lambda x: x.name)],
            "dependencies": [{"plugin": d.dep_plugin, "spec": d.spec} for d in
                            sorted(pv.dependencies, key=lambda x: x.dep_plugin)],
            "conflicts": sorted(c.conflict_plugin for c in pv.conflicts),
            "package_url": f"/api/v1/plugins/{name}/versions/{version}/download",
            "published_revision": pv.published_revision,
            "revoked_revision": pv.revoked_revision,
            "revoke_reason": pv.revoke_reason,
            "revoked": pv.revoked_revision is not None,
        })
    payload = {
        "batch_id": outcome.batch_id,
        "revision": outcome.revision,
        "cached": outcome.cached,
        "content_hash": outcome.content_hash,
        "member_count": len(members),
        "members": members,
        "created_at": outcome.batch_row.created_at.isoformat()
        if outcome.batch_row is not None else None,
    }
    return JSONResponse(status_code=status_code, content=BatchOut(**payload).model_dump(mode="json"))


# ------------------------------------------------------------ 目录浏览
@router.get("/revisions", response_model=list[RevisionOut], tags=["catalog"])
def list_revisions(limit: int = 100, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 1000))
    rows = db.execute(
        select(CatalogRevision).order_by(CatalogRevision.revision.desc()).limit(limit)
    ).scalars()
    return [RevisionOut(
        revision=r.revision, created_at=r.created_at, kind=r.kind,
        plugin=r.plugin, version=r.version, note=r.note) for r in rows]


@router.get("/revisions/{revision}", response_model=RevisionOut, tags=["catalog"])
def get_revision(revision: int, db: Session = Depends(get_db)):
    r = db.get(CatalogRevision, revision)
    if r is None:
        raise HTTPException(404, f"修订 {revision} 不存在")
    return RevisionOut(
        revision=r.revision, created_at=r.created_at, kind=r.kind,
        plugin=r.plugin, version=r.version, note=r.note)


@router.get("/plugins", tags=["catalog"])
def list_plugins(revision: int | None = None, db: Session = Depends(get_db)):
    """列出插件及其版本；给定 revision 时只显示该修订下未撤销版本。"""
    if revision is None:
        base_rev = repository.latest_revision(db)
    else:
        base_rev = revision
        if db.get(CatalogRevision, revision) is None:
            raise HTTPException(404, f"修订 {revision} 不存在")
    pvs = db.execute(
        select(PluginVersion, Plugin.name)
        .join(Plugin, Plugin.id == PluginVersion.plugin_id)
        .where(
            PluginVersion.published_revision <= base_rev,
            (PluginVersion.revoked_revision.is_(None))
            | (PluginVersion.revoked_revision > base_rev),
        )
        .order_by(Plugin.name, PluginVersion.published_revision)
    ).all()
    out: dict[str, list[str]] = {}
    for pv, name in pvs:
        out.setdefault(name, []).append(pv.version)
    return {"revision": base_rev, "plugins": out}


@router.get("/plugins/{plugin_name}/versions/{version}",
            response_model=VersionOut, tags=["catalog"])
def get_version(plugin_name: str, version: str, db: Session = Depends(get_db)):
    pv = db.execute(
        select(PluginVersion).join(Plugin, Plugin.id == PluginVersion.plugin_id)
        .where(Plugin.name == plugin_name, PluginVersion.version == version)
    ).scalar_one_or_none()
    if pv is None:
        raise HTTPException(404, f"未找到 {plugin_name}=={version}")
    return _version_out(db, pv)


@router.get("/plugins/{plugin_name}/versions/{version}/download",
            tags=["catalog"], include_in_schema=True)
def download(plugin_name: str, version: str, db: Session = Depends(get_db)):
    pv = db.execute(
        select(PluginVersion).join(Plugin, Plugin.id == PluginVersion.plugin_id)
        .where(Plugin.name == plugin_name, PluginVersion.version == version)
    ).scalar_one_or_none()
    if pv is None or not os.path.exists(pv.storage_path):
        raise HTTPException(404, "安装包不存在")
    return FileResponse(
        pv.storage_path, filename=pv.filename,
        headers={
            "X-SHA256": pv.sha256,
            "X-Revoked": "true" if pv.revoked_revision else "false",
        },
    )


# ------------------------------------------------------------ 求解

@router.post("/solutions", response_model=SolutionOut, tags=["solve"])
def solve(body: SolveRequest, db: Session = Depends(get_db),
          authorization: str | None = Header(default=None)):
    _check_token(authorization)

    # 关键：求解必须固定在同一目录修订上。
    # PostgreSQL 用 REPEATABLE READ 事务快照；其它方言（测试用 SQLite）
    # 的单写连接天然等价，跳过该选项以免方言报错。
    _set_snapshot_isolation(db)

    try:
        revision = service.resolve_revision(db, body.revision)
    except RequestError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    request_hash = _safe_request_hash(body)

    try:
        outcome = service.execute(
            db,
            revision=revision,
            runtimes_in=body.runtimes,
            targets_in=body.targets,
        )
    except RequestError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except (UnsatisfiableError, CycleError) as e:
        db.rollback()
        code = "dependency_cycle" if isinstance(e, CycleError) else "unsatisfiable"
        status = 422 if isinstance(e, CycleError) else 409
        raise HTTPException(status_code=status, detail={
            "detail": str(e),
            "code": code,
            "revision": revision,
            "request_hash": request_hash,
            "conflicts": e.reasons,
        })
    except InvalidSpecifier as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    try:
        db.commit()
    except Exception as exc:
        # 并发下唯一约束冲突（两个事务同时算出同一方案）：重读既有方案
        db.rollback()
        existing = repository.get_solution(db, outcome.revision, request_hash)
        if existing is None:
            raise HTTPException(status_code=500, detail=f"方案提交失败: {exc}")
        outcome = type(outcome)(
            solution_row=existing, cached=True,
            selection=existing.selection, install_order=existing.install_order,
            revision=outcome.revision)

    row = outcome.solution_row
    selection = annotate_revoked(db, outcome.selection, outcome.revision)
    return SolutionOut(
        solution_id=row.id,
        revision=outcome.revision,
        cached=outcome.cached,
        request_hash=row.request_hash,
        selection=_ordered_selection(selection),
        install_order=outcome.install_order,
        created_at=row.created_at,
    )


@router.get("/solutions/{solution_id}", response_model=SolutionOut, tags=["solve"])
def get_solution(solution_id: int, db: Session = Depends(get_db)):
    row = db.get(Solution, solution_id)
    if row is None:
        raise HTTPException(404, f"方案 {solution_id} 不存在")
    selection = annotate_revoked(db, row.selection)
    return SolutionOut(
        solution_id=row.id,
        revision=row.revision_id,
        cached=True,
        request_hash=row.request_hash,
        selection=_ordered_selection(selection),
        install_order=row.install_order,
        created_at=row.created_at,
    )


# ------------------------------------------------------------ 方案预演（不保存）

def _set_snapshot_isolation(db: Session) -> None:
    """预演/求解固定在同一目录修订快照上（PostgreSQL REPEATABLE READ）。"""
    if db.bind.dialect.name == "postgresql":
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def _safe_preview_request_hash(row: Solution | None, body: PreviewRequest) -> str:
    """对预演的合并后有效请求计算规范化哈希，供失败回显；解析失败返回空串。"""
    if row is None:
        return ""
    try:
        (_, targets_plain, runtime_reqs, _, _) = service.effective_request(
            row, runtimes_in=body.runtimes, targets_in=body.targets)
        h, _ = catalog.canonical_request(runtime_reqs, targets_plain)
        return h
    except RequestError:
        return ""


@router.post("/solutions/{solution_id}/preview", response_model=PreviewOut,
             tags=["solve"])
def preview_solution(solution_id: int, body: PreviewRequest,
                     db: Session = Depends(get_db)):
    """对已保存方案在目标修订上做只读预演：最小变动优先，不保存任何结果。"""
    _set_snapshot_isolation(db)

    try:
        revision = service.resolve_revision(db, body.revision)
    except RequestError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    row = db.get(Solution, solution_id)
    if row is None:
        db.rollback()
        raise HTTPException(status_code=404, detail=f"方案 {solution_id} 不存在")

    request_hash = _safe_preview_request_hash(row, body)

    try:
        outcome = service.preview(
            db,
            solution_id=solution_id,
            revision=revision,
            runtimes_in=body.runtimes,
            targets_in=body.targets,
        )
    except RequestError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except SolutionNotFound as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e))
    except (UnsatisfiableError, CycleError) as e:
        db.rollback()
        code = "dependency_cycle" if isinstance(e, CycleError) else "unsatisfiable"
        status = 422 if isinstance(e, CycleError) else 409
        raise HTTPException(status_code=status, detail={
            "detail": str(e),
            "code": code,
            "revision": revision,
            "source_solution_id": solution_id,
            "request_hash": request_hash,
            "conflicts": e.reasons,
        })
    except InvalidSpecifier as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    # 预演不写任何东西：显式回滚结束只读事务
    db.rollback()

    selection = annotate_revoked(db, outcome.selection, outcome.revision)
    return PreviewOut(
        source_solution_id=outcome.source_solution_id,
        revision=outcome.revision,
        saved=False,
        request_hash=outcome.request_hash,
        runtimes=outcome.effective_runtimes,
        targets=outcome.effective_targets,
        selection=_ordered_selection(selection),
        install_order=outcome.install_order,
        change_counts=outcome.change_counts,
        changes={k: outcome.changes[k] for k in sorted(outcome.changes)},
    )


# ------------------------------------------------------------ 辅助

def _ordered_selection(selection: dict) -> dict:
    """pydantic 模型按插件名有序输出，保证响应确定性。"""
    return {k: selection[k] for k in sorted(selection)}


def _pydantic_errors(exc: ValueError):
    # PublishMetadata(**data) 抛出的校验错误可能是 ValidationError
    errs = getattr(exc, "errors", None)
    if callable(errs):
        try:
            return errs()
        except Exception:
            pass
    return str(exc)


def _version_out(db: Session, pv: PluginVersion) -> VersionOut:
    plugin = db.get(Plugin, pv.plugin_id)
    return VersionOut(
        plugin=plugin.name,
        version=pv.version,
        sha256=pv.sha256,
        filename=pv.filename,
        size_bytes=pv.size_bytes,
        runtimes=[{"name": r.name, "spec": r.spec} for r in
                  sorted(pv.runtimes, key=lambda x: x.name)],
        dependencies=[{"plugin": d.dep_plugin, "spec": d.spec} for d in
                      sorted(pv.dependencies, key=lambda x: x.dep_plugin)],
        conflicts=sorted(c.conflict_plugin for c in pv.conflicts),
        published_revision=pv.published_revision,
        revoked_revision=pv.revoked_revision,
        revoke_reason=pv.revoke_reason,
        revoked=pv.revoked_revision is not None,
        created_at=pv.created_at,
    )
