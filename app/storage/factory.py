from app.core.config import Settings
from app.storage.base import ObjectStorage


def build_storage(settings: Settings) -> ObjectStorage:
    if settings.storage_backend == "s3":
        from app.storage.s3 import S3Storage
        return S3Storage(settings)
    from app.storage.local import LocalFsStorage
    return LocalFsStorage(settings.local_storage_dir)
