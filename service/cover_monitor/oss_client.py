from __future__ import annotations

from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit


class AliyunOssV2Client:
    def __init__(self, client: Any, sdk: Any) -> None:
        self._client = client
        self._sdk = sdk

    def object_exists(self, bucket: str, key: str) -> bool:
        return bool(self._client.is_object_exist(bucket=bucket, key=key))

    def put_object(self, bucket: str, key: str, content: bytes) -> None:
        request = self._sdk.PutObjectRequest(bucket=bucket, key=key, body=content)
        self._client.put_object(request)

    def download_object(self, bucket: str, key: str, target: Path) -> None:
        request = self._sdk.GetObjectRequest(bucket=bucket, key=key)
        self._client.get_object_to_file(request, str(target))

def build_aliyun_oss_v2_client(
    *,
    region: str,
    endpoint: str = "",
    credential_mode: str = "ecs_ram_role",
    ecs_role_name: str = "",
) -> AliyunOssV2Client:
    region_name = str(region or "").strip()
    if not re.fullmatch(r"[a-z0-9-]{3,64}", region_name):
        raise ValueError("OSS region 配置非法或缺失")
    endpoint_value = str(endpoint or "").strip()
    if endpoint_value:
        parts = urlsplit(endpoint_value)
        if parts.scheme != "https" or not parts.hostname or parts.path not in {"", "/"}:
            raise ValueError("OSS endpoint 必须是 HTTPS 服务根地址")
    mode = str(credential_mode or "ecs_ram_role").strip().lower()
    if mode not in {"environment", "ecs_ram_role"}:
        raise ValueError(f"不支持的 OSS 凭证模式: {mode}")

    try:
        import alibabacloud_oss_v2 as oss
    except ImportError as exc:
        raise RuntimeError("OSS 后端需要安装 alibabacloud-oss-v2") from exc

    if mode == "environment":
        try:
            credentials_provider = oss.credentials.EnvironmentVariableCredentialsProvider()
        except Exception as exc:
            raise RuntimeError("OSS 环境变量凭证不可用") from exc
    elif mode == "ecs_ram_role":
        try:
            from alibabacloud_credentials.client import Client as CredentialClient
            from alibabacloud_credentials.models import Config as CredentialConfig
        except ImportError as exc:
            raise RuntimeError("ECS RAM 角色需要安装 alibabacloud-credentials") from exc
        options: dict[str, Any] = {"type": "ecs_ram_role"}
        role_name = str(ecs_role_name or "").strip()
        if role_name:
            options["role_name"] = role_name
        credential_client = CredentialClient(CredentialConfig(**options))

        def load_credentials():
            credential = credential_client.get_credential()
            return oss.credentials.Credentials(
                access_key_id=credential.access_key_id,
                access_key_secret=credential.access_key_secret,
                security_token=credential.security_token,
            )

        credentials_provider = oss.credentials.CredentialsProviderFunc(func=load_credentials)
    config = oss.config.load_default()
    config.credentials_provider = credentials_provider
    config.region = region_name
    if endpoint_value:
        config.endpoint = endpoint_value
    return AliyunOssV2Client(oss.Client(config), oss)
