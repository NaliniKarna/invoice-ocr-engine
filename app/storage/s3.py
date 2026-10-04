from __future__ import annotations

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from typing import Iterator

from app.storage.base import ObjectInfo, ObjectNotFound, StorageError, validate_key


class S3Storage:
    """Works against AWS S3 and MinIO (set OCR_S3_ENDPOINT_URL). Business code never sees boto3."""

    def __init__(self, settings: Settings):
        self.bucket = settings.s3_bucket
        self.sse = settings.s3_sse
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key.get_secret_value() if settings.s3_access_key else None,
            aws_secret_access_key=settings.s3_secret_key.get_secret_value() if settings.s3_secret_key else None,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"},
                          s3={"addressing_style": "path" if settings.s3_endpoint_url else "auto"},
                          connect_timeout=5, read_timeout=30),
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        extra = {"ServerSideEncryption": self.sse} if self.sse else {}
        try:
            self.client.put_object(Bucket=self.bucket, Key=validate_key(key), Body=data,
                                   ContentType=content_type, **extra)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("put failed") from exc

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=validate_key(key))["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise ObjectNotFound(key) from exc
            raise StorageError("get failed") from exc
        except BotoCoreError as exc:
            raise StorageError("get failed") from exc

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=validate_key(key))
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("delete failed") from exc

    def ping(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("bucket unreachable") from exc

    def list_objects(self, prefix: str) -> Iterator[ObjectInfo]:
        try:
            for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                for o in page.get("Contents", []):
                    yield ObjectInfo(o["Key"], o["LastModified"], o["Size"])
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("list failed") from exc
