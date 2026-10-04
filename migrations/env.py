import os

from alembic import context
from sqlalchemy import create_engine

import app.models  # noqa: F401  (register tables)
from app.db.base import Base

config = context.config
target_metadata = Base.metadata
URL = os.environ.get("OCR_DATABASE_URL", "postgresql+psycopg://ocr:ocr@localhost:5432/ocr")


def run_migrations_offline():
    context.configure(url=URL, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = create_engine(URL)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


(run_migrations_offline if context.is_offline_mode() else run_migrations_online)()
