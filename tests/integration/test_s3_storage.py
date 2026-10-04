import boto3
import pytest
from moto import mock_aws

from app.core.config import Settings
from app.storage.base import ObjectNotFound
from app.storage.s3 import S3Storage


@pytest.fixture
def s3():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="bill-ocr")
        yield S3Storage(Settings(env="test", api_key_pepper="p" * 32, admin_token="a" * 32, s3_access_key="k",
                                 s3_secret_key="s", s3_bucket="bill-ocr", s3_sse="AES256"))


def test_roundtrip_delete_and_sse(s3):
    s3.put("tenants/c/documents/p/original.png", b"data", "image/png")
    assert s3.get("tenants/c/documents/p/original.png") == b"data"
    head = s3.client.head_object(Bucket="bill-ocr", Key="tenants/c/documents/p/original.png")
    assert head["ServerSideEncryption"] == "AES256"
    s3.delete("tenants/c/documents/p/original.png")
    with pytest.raises(ObjectNotFound):
        s3.get("tenants/c/documents/p/original.png")
    s3.ping()
