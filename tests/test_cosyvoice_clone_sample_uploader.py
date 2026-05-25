"""Phase 4.1.x Aliyun OSS sample uploader tests.

These tests never touch real OSS. They inject a fake boto-style client directly
into ``AliyunOssUploader`` and assert the contract at the object-store boundary.
"""
from __future__ import annotations

import inspect

from cosyvoice_clone.sample_uploader import (
    AliyunOssUploader,
    IMPLEMENTED_BACKENDS,
    PRODUCTION_READY_BACKENDS,
    _object_key_from_signed_url,
    missing_aliyun_oss_settings,
)


class _FakeOssClient:
    def __init__(self) -> None:
        self.put_calls: list[dict] = []
        self.presign_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)

    def generate_presigned_url(self, method, *, Params, ExpiresIn, HttpMethod):
        self.presign_calls.append({
            "method": method,
            "Params": Params,
            "ExpiresIn": ExpiresIn,
            "HttpMethod": HttpMethod,
        })
        return (
            "https://avt-cosyvoice-test.s3.oss-cn-beijing.aliyuncs.com/"
            f"{Params['Key']}?signed=1"
        )

    def delete_object(self, **kwargs):
        self.delete_calls.append(kwargs)


def _make_uploader() -> tuple[AliyunOssUploader, _FakeOssClient]:
    fake = _FakeOssClient()
    uploader = AliyunOssUploader(
        endpoint="https://s3.oss-cn-beijing.aliyuncs.com",
        bucket="avt-cosyvoice-test",
        access_key_id="ak-test",
        access_key_secret="secret-test",
        region="cn-beijing",
        key_prefix="cosyvoice/clone-samples",
    )
    uploader._client = fake
    return uploader, fake


def test_aliyun_oss_backend_is_implemented_and_production_ready() -> None:
    assert "aliyun_oss" in IMPLEMENTED_BACKENDS
    assert "aliyun_oss" in PRODUCTION_READY_BACKENDS
    assert "local_fs_stub" not in PRODUCTION_READY_BACKENDS


def test_upload_and_sign_puts_object_then_returns_presigned_get_url() -> None:
    uploader, fake = _make_uploader()
    data = b"RIFF....WAVE" * 8

    url = uploader.upload_and_sign(data, filename_hint="voice.wav", ttl_seconds=1800)

    assert url.startswith("https://avt-cosyvoice-test.s3.oss-cn-beijing.aliyuncs.com/")
    assert fake.put_calls and fake.presign_calls
    put = fake.put_calls[0]
    assert put["Bucket"] == "avt-cosyvoice-test"
    assert put["Key"].startswith("cosyvoice/clone-samples/")
    assert put["Key"].endswith(".wav")
    assert put["Body"] == data
    assert put["ContentType"] == "audio/wav"
    assert put["Metadata"]["sha256"]

    presign = fake.presign_calls[0]
    assert presign["method"] == "get_object"
    assert presign["Params"]["Bucket"] == "avt-cosyvoice-test"
    assert presign["Params"]["Key"] == put["Key"]
    assert presign["Params"]["ResponseContentType"] == "audio/wav"
    assert presign["ExpiresIn"] == 1800
    assert presign["HttpMethod"] == "GET"


def test_delete_uploaded_url_deletes_the_same_object_key() -> None:
    uploader, fake = _make_uploader()
    url = uploader.upload_and_sign(b"abc", filename_hint="voice.wav", ttl_seconds=60)

    uploader.delete_uploaded_url(url)

    assert fake.delete_calls == [{
        "Bucket": "avt-cosyvoice-test",
        "Key": fake.put_calls[0]["Key"],
    }]


def test_delete_uploaded_url_can_parse_virtual_hosted_signed_url() -> None:
    uploader, fake = _make_uploader()
    url = (
        "https://avt-cosyvoice-test.s3.oss-cn-beijing.aliyuncs.com/"
        "cosyvoice/clone-samples/2026/05/25/a%20b.wav?Expires=1"
    )

    uploader.delete_uploaded_url(url)

    assert fake.delete_calls == [{
        "Bucket": "avt-cosyvoice-test",
        "Key": "cosyvoice/clone-samples/2026/05/25/a b.wav",
    }]


def test_missing_aliyun_oss_settings_returns_env_names() -> None:
    class _S:
        cosyvoice_oss_endpoint = ""
        cosyvoice_oss_bucket = "bucket"
        cosyvoice_oss_access_key_id = ""
        cosyvoice_oss_access_key_secret = "secret"

    assert missing_aliyun_oss_settings(_S()) == [
        "AVT_COSYVOICE_OSS_ENDPOINT",
        "AVT_COSYVOICE_OSS_ACCESS_KEY_ID",
    ]


def test_aliyun_oss_client_uses_oss_compatible_s3_config() -> None:
    source = inspect.getsource(AliyunOssUploader._get_client)

    assert 'signature_version="s3"' in source
    assert '"addressing_style": "virtual"' in source
    assert '"max_attempts": 1' in source


def test_object_key_from_signed_url_extracts_path_without_query() -> None:
    assert _object_key_from_signed_url(
        "https://bucket.s3.oss-cn-beijing.aliyuncs.com/a/b.wav?x=1"
    ) == "a/b.wav"
