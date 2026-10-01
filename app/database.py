"""数据库引擎与会话。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

# PostgreSQL 服务端使用连接池；SQLite（离线测试）使用内置线程池，
# 不接受 pool_size/max_overflow 参数。
if settings.database_url.startswith("sqlite"):
    engine = create_engine(settings.database_url)
else:
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=int(__import__("os").environ.get("DB_POOL_SIZE", "10")),
        max_overflow=20,
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI 依赖：每个请求一个 Session。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
