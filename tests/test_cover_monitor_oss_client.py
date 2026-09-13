from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from service.cover_monitor.oss_client import AliyunOssV2Client, build_aliyun_oss_v2_client


class Request:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class FakeRawClient:
    def __init__(self) -> None:
        self.exists = True
        self.put_request = None
        self.download_request = None
        self.download_target = ""

    def is_object_exist(self, *, bucket: str, key: str) -> bool:
        return self.exists and bucket == "private-covers" and key == "cover/item.jpg"

    def put_object(self, request) -> None:
        self.put_request = request

    def get_object_to_file(self, request, target: str) -> None:
        self.download_request = request
        self.download_target = target

def test_aliyun_v2_adapter_translates_storage_operations(tmp_path: Path) -> None:
    raw = FakeRawClient()
    sdk = SimpleNamespace(PutObjectRequest=Request, GetObjectRequest=Request)
    client = AliyunOssV2Client(raw, sdk)

    assert client.object_exists("private-covers", "cover/item.jpg") is True
    client.put_object("private-covers", "cover/item.jpg", b"content")
    client.download_object("private-covers", "cover/item.jpg", tmp_path / "cover.jpg")

    assert raw.put_request.body == b"content"
    assert raw.download_request.key == "cover/item.jpg"
    assert raw.download_target == str(tmp_path / "cover.jpg")


def test_aliyun_client_factory_builds_environment_credentials(monkeypatch) -> None:
    provider = object()
    config = SimpleNamespace(credentials_provider=None, region="", endpoint="")
    raw_client = FakeRawClient()
    fake_sdk = SimpleNamespace(
        credentials=SimpleNamespace(EnvironmentVariableCredentialsProvider=lambda: provider),
        config=SimpleNamespace(load_default=lambda: config),
        Client=lambda value: raw_client if value is config else None,
        PutObjectRequest=Request,
        GetObjectRequest=Request,
    )
    monkeypatch.setitem(sys.modules, "alibabacloud_oss_v2", fake_sdk)

    client = build_aliyun_oss_v2_client(
        region="cn-hangzhou",
        endpoint="https://oss-cn-hangzhou-internal.aliyuncs.com",
        credential_mode="environment",
    )

    assert isinstance(client, AliyunOssV2Client)
    assert config.credentials_provider is provider
    assert config.region == "cn-hangzhou"
    assert config.endpoint == "https://oss-cn-hangzhou-internal.aliyuncs.com"


def test_aliyun_client_factory_reports_missing_environment_credentials(monkeypatch) -> None:
    class MissingEnvironmentCredentials:
        def __init__(self) -> None:
            raise RuntimeError("raw provider failure")

    fake_sdk = SimpleNamespace(
        credentials=SimpleNamespace(
            EnvironmentVariableCredentialsProvider=MissingEnvironmentCredentials
        ),
        config=SimpleNamespace(load_default=lambda: SimpleNamespace()),
        Client=lambda _config: FakeRawClient(),
    )
    monkeypatch.setitem(sys.modules, "alibabacloud_oss_v2", fake_sdk)

    with pytest.raises(RuntimeError, match="OSS 环境变量凭证不可用") as captured:
        build_aliyun_oss_v2_client(
            region="cn-hangzhou",
            credential_mode="environment",
        )

    assert "raw provider failure" not in str(captured.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"region": ""},
        {"region": "cn-hangzhou", "endpoint": "http://oss-cn-hangzhou.aliyuncs.com"},
        {"region": "cn-hangzhou", "credential_mode": "unknown"},
    ],
)
def test_aliyun_client_factory_rejects_unsafe_configuration(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        build_aliyun_oss_v2_client(**kwargs)
