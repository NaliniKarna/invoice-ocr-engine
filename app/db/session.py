from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings


def make_engine(settings: Settings) -> Engine:
    url = settings.database_url
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):  # enforce FKs in SQLite so tests exercise them
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
        return engine
    # Server-side timeouts bound every statement/lock wait so a stuck query cannot pin a pooled connection.
    opts = (f"-c statement_timeout={settings.db_statement_timeout_ms} -c lock_timeout={settings.db_lock_timeout_ms} "
            f"-c idle_in_transaction_session_timeout={settings.db_idle_in_tx_timeout_ms}")
    return create_engine(url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow,
                         pool_timeout=settings.db_pool_timeout_s, pool_pre_ping=True, pool_recycle=1800,
                         connect_args={"options": opts, "connect_timeout": 5})


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    db = factory()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
