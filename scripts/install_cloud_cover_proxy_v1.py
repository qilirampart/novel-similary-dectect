from __future__ import annotations

import argparse
import gzip
import getpass
import hashlib
import io
import json
import os
import platform
import posixpath
import re
import shlex
import socket
import stat
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

import paramiko


MIHOMO_VERSION = "v1.19.30"
REMOTE_BINARY = "/usr/local/bin/mihomo-cover"
REMOTE_CONFIG_DIR = "/etc/mihomo-cover"
REMOTE_STATE_DIR = "/var/lib/mihomo-cover"
REMOTE_PROVIDER = f"{REMOTE_STATE_DIR}/providers/subscription.yaml"
STALE_PROVIDER = f"{REMOTE_CONFIG_DIR}/providers/subscription.yaml"
REMOTE_CONFIG = f"{REMOTE_CONFIG_DIR}/config.yaml"
REMOTE_SERVICE = "/etc/systemd/system/mihomo-cover.service"
PROXY_URL = "http://127.0.0.1:17890"
EXPECTED_SHA256 = {
    "mihomo-linux-amd64-v1.19.30.gz": "cf06ce2c7d1421bdbda14ee4a5b6046672dc35ebf8eecd8e77504ec3c0ed9a84",
}
PINNED_SS_SERVER_POOLS = {
    "gzdata1.233netpro.com": [
        "45.141.170.37", "45.141.170.38", "45.141.170.57", "45.141.170.58",
        "45.141.171.37", "45.141.171.38", "45.141.171.57", "45.141.171.58",
    ],
    "shdata1.233netpro.com": [
        "45.141.170.37", "45.141.170.38", "45.141.170.57", "45.141.170.58",
        "45.141.171.37", "45.141.171.38", "45.141.171.57", "45.141.171.58",
    ],
}


def _ascii_value(resource_file: Path, line_number: int) -> str:
    lines = resource_file.read_bytes().splitlines()
    if line_number >= len(lines):
        raise ValueError(f"resource file has no line {line_number + 1}")
    matches = re.findall(rb"[A-Za-z0-9._-]{4,}", lines[line_number])
    if not matches:
        raise ValueError(f"resource file line {line_number + 1} has no ASCII value")
    return matches[-1].decode("ascii")


class RemoteSession:
    def __init__(self, host: str, user: str, password: str) -> None:
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
        )
        self._sftp = self._client.open_sftp()

    def close(self) -> None:
        try:
            self._sftp.close()
        finally:
            self._client.close()

    def run(self, command: str, *, timeout: int = 60, allow_failure: bool = False) -> str:
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if exit_status != 0 and not allow_failure:
            detail = "\n".join(part for part in (out[-2000:], err[-2000:]) if part.strip())
            raise RuntimeError(f"Remote command failed ({exit_status}): {detail}")
        return out if out.strip() else err

    def write_bytes(self, remote_path: str, content: bytes, mode: int) -> None:
        self.run(f"mkdir -p {shlex.quote(posixpath.dirname(remote_path))}")
        with self._sftp.open(remote_path, "wb") as remote_file:
            remote_file.write(content)
        self._sftp.chmod(remote_path, mode)

    def remove_file(self, remote_path: str) -> None:
        try:
            self._sftp.remove(remote_path)
        except OSError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install an isolated cloud proxy for cover collection.")
    parser.add_argument("--resource-file", type=Path, required=True)
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--subscription-stdin", action="store_true")
    parser.add_argument("--subscription-proxy", default="")
    parser.add_argument("--provider-file", type=Path)
    return parser.parse_args()


def inspect_remote(remote: RemoteSession) -> dict[str, str]:
    commands = {
        "os": "uname -a; printf '\\n'; cat /etc/os-release | head -n 8",
        "resources": "nproc; free -m; df -h /",
        "routes": "ip route show; printf '\\nDNS\\n'; cat /etc/resolv.conf",
        "listeners": "ss -lntup",
        "services": "systemctl list-units --type=service --state=running --no-pager --no-legend",
        "containers": "docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null || true",
        "proxy_environment": (
            "systemctl show-environment | grep -iE '(^|_)https?_proxy=|all_proxy=|no_proxy=' || true; "
            "printf '\\nPROFILE_PROXY_SETTINGS\\n'; "
            "grep -RniE 'https?_proxy|all_proxy' /etc/environment /etc/profile /etc/profile.d 2>/dev/null || true"
        ),
        "mihomo": (
            "systemctl show mihomo-cover.service "
            "--property=LoadState,ActiveState,SubState,MainPID,MemoryCurrent --no-pager 2>/dev/null || true"
        ),
    }
    return {name: remote.run(command, allow_failure=True).strip() for name, command in commands.items()}


def _linux_asset_name(machine: str) -> str:
    normalized = machine.lower()
    if normalized in {"x86_64", "amd64"}:
        return f"mihomo-linux-amd64-{MIHOMO_VERSION}.gz"
    if normalized in {"aarch64", "arm64"}:
        return f"mihomo-linux-arm64-{MIHOMO_VERSION}.gz"
    raise RuntimeError(f"Unsupported cloud architecture: {machine}")


def _download(url: str, *, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "novel-similarity-deployer/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _download_subscription(url: str, *, proxy_url: str = "") -> bytes:
    headers = {
        "User-Agent": "clash-verge/v2.4.2",
        "Accept": "text/yaml, application/yaml, text/plain, */*",
    }
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        if proxy_url
        else urllib.request.ProxyHandler({})
    )
    try:
        with opener.open(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        nested_urls = urllib.parse.parse_qs(
            urllib.parse.urlparse(url).query
        ).get("url", [])
        if exc.code == 403 and nested_urls:
            parsed = urllib.parse.urlparse(url)
            if parsed.hostname == "api-huacloud.com":
                alternate = parsed._replace(netloc="api-huacloud.net").geturl()
                alternate_request = urllib.request.Request(alternate, headers=headers)
                try:
                    with opener.open(alternate_request, timeout=120) as response:
                        return response.read()
                except urllib.error.HTTPError:
                    pass
            nested_request = urllib.request.Request(nested_urls[0], headers=headers)
            try:
                with opener.open(nested_request, timeout=120) as response:
                    return response.read()
            except urllib.error.HTTPError as nested_exc:
                nested_body = nested_exc.read(512).decode("utf-8", errors="replace")
                nested_body = re.sub(r"https?://[^\s<>'\"]+", "[url]", nested_body)
                raise RuntimeError(
                    f"Raw subscription returned HTTP {nested_exc.code}: "
                    f"{nested_body[:300].strip()}"
                ) from nested_exc
        body = exc.read(512).decode("utf-8", errors="replace")
        body = re.sub(r"https?://[^\s<>'\"]+", "[url]", body)
        raise RuntimeError(
            f"Subscription endpoint returned HTTP {exc.code}: {body[:300].strip()}"
        ) from exc


def _extract_provider_yaml(subscription: bytes) -> bytes:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to sanitize the subscription") from exc
    payload = yaml.safe_load(subscription.decode("utf-8-sig"))
    proxies = payload.get("proxies") if isinstance(payload, dict) else None
    if not isinstance(proxies, list) or not proxies:
        raise RuntimeError("Subscription has no usable proxies")
    pinned_proxies, pinned_domains, dropped_proxies = _pin_ss_server_domains(proxies)
    if not pinned_proxies:
        raise RuntimeError("Subscription has no proxies with resolvable servers")
    print(f"pinned_domains={pinned_domains}")
    print(f"dropped_unresolvable_proxies={dropped_proxies}")
    sanitized = {"proxies": pinned_proxies}
    return yaml.safe_dump(sanitized, allow_unicode=True, sort_keys=False).encode("utf-8")


def _resolve_a_records(domain: str) -> list[str]:
    if domain in PINNED_SS_SERVER_POOLS:
        return list(PINNED_SS_SERVER_POOLS[domain])
    try:
        addresses = sorted({
            item[4][0]
            for item in socket.getaddrinfo(domain, None, socket.AF_INET)
        })
        if addresses:
            return addresses
    except OSError:
        pass
    if platform.system() != "Windows" or not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
        return []
    result = subprocess.run(
        ["nslookup", domain, "223.5.5.5"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return []
    addresses = []
    candidates = re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", result.stdout)
    for candidate in candidates:
        if candidate == "223.5.5.5":
            continue
        try:
            import ipaddress

            ipaddress.IPv4Address(candidate)
            addresses.append(candidate)
        except ValueError:
            continue
    return sorted(set(addresses))


def _pin_ss_server_domains(proxies: list[dict]) -> tuple[list[dict], int, int]:
    resolved: dict[str, list[str]] = {}
    pinned: list[dict] = []
    dropped = 0
    for proxy in proxies:
        item = dict(proxy)
        server = str(item.get("server") or "").strip()
        if not server or re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", server):
            pinned.append(item)
            continue
        proxy_type = str(item.get("type") or "").lower()
        if server not in resolved:
            resolved[server] = _resolve_a_records(server)
        addresses = resolved[server]
        if not addresses:
            dropped += 1
            continue
        seed = f"{item.get('name', '')}:{item.get('port', '')}".encode("utf-8")
        item["server"] = addresses[int(hashlib.sha256(seed).hexdigest(), 16) % len(addresses)]
        if proxy_type == "vless" and not item.get("servername"):
            item["servername"] = server
        elif proxy_type == "hysteria2" and not item.get("sni"):
            item["sni"] = server
        pinned.append(item)
    return pinned, sum(1 for addresses in resolved.values() if addresses), dropped


def build_config() -> bytes:
    config = f"""mixed-port: 17890
allow-lan: false
bind-address: 127.0.0.1
mode: rule
log-level: warning
ipv6: false
tcp-concurrent: true
dns:
  enable: true
  ipv6: false
  enhanced-mode: redir-host
  nameserver:
    - 223.5.5.5
    - 119.29.29.29
profile:
  store-selected: false
  store-fake-ip: false
proxy-providers:
  cover-subscription:
    type: file
    path: {REMOTE_PROVIDER}
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
proxy-groups:
  - name: COVER-AUTO
    type: url-test
    use:
      - cover-subscription
    url: https://www.gstatic.com/generate_204
    interval: 300
    tolerance: 80
rules:
  - MATCH,COVER-AUTO
"""
    return config.encode("utf-8")


def build_service() -> bytes:
    service = f"""[Unit]
Description=Isolated Mihomo proxy for cover collection
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mihomo-cover
Group=mihomo-cover
WorkingDirectory={REMOTE_STATE_DIR}
ExecStart={REMOTE_BINARY} -d {REMOTE_STATE_DIR} -f {REMOTE_CONFIG}
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ReadWritePaths={REMOTE_STATE_DIR}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
LockPersonality=true
MemoryDenyWriteExecute=false
LimitNOFILE=16384
MemoryMax=256M
CPUQuota=50%

[Install]
WantedBy=multi-user.target
"""
    return service.encode("utf-8")


def install_remote(
    remote: RemoteSession,
    *,
    read_subscription_from_stdin: bool,
    subscription_proxy: str,
    provider_file: Path | None,
) -> None:
    if provider_file:
        provider_source = provider_file.read_bytes()
    else:
        subscription_url = os.environ.get("COVER_PROXY_SUBSCRIPTION_URL", "").strip()
        if not subscription_url and read_subscription_from_stdin:
            subscription_url = getpass.getpass("Subscription URL: ").strip()
        if not subscription_url:
            raise RuntimeError("A subscription URL or --provider-file is required")
        provider_source = _download_subscription(
            subscription_url,
            proxy_url=subscription_proxy,
        )

    machine = remote.run("uname -m").strip()
    asset = _linux_asset_name(machine)
    installed_version = remote.run(f"{REMOTE_BINARY} -v", allow_failure=True)
    binary: bytes | None = None
    actual_sha256 = "previously_verified"
    if MIHOMO_VERSION.lstrip("v") not in installed_version:
        release_url = f"https://github.com/MetaCubeX/mihomo/releases/download/{MIHOMO_VERSION}/{asset}"
        compressed_binary = _download(release_url)
        expected_sha256 = EXPECTED_SHA256.get(asset)
        actual_sha256 = hashlib.sha256(compressed_binary).hexdigest()
        if not expected_sha256 or actual_sha256 != expected_sha256:
            raise RuntimeError(f"Mihomo checksum mismatch for {asset}")
        binary = gzip.GzipFile(fileobj=io.BytesIO(compressed_binary)).read()
        if not binary.startswith(b"\x7fELF"):
            raise RuntimeError("Downloaded Mihomo asset is not an ELF binary")

    provider = _extract_provider_yaml(provider_source)
    print(f"mihomo_asset={asset}")
    print(f"mihomo_sha256={actual_sha256}")
    print(f"provider_bytes={len(provider)}")

    remote.run(
        "id -u mihomo-cover >/dev/null 2>&1 || "
        f"useradd --system --home-dir {REMOTE_STATE_DIR} --shell /sbin/nologin mihomo-cover"
    )
    remote.run(f"mkdir -p {REMOTE_STATE_DIR}")
    if binary is not None:
        remote.write_bytes(REMOTE_BINARY, binary, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    remote.write_bytes(REMOTE_PROVIDER, provider, stat.S_IRUSR | stat.S_IWUSR)
    remote.write_bytes(REMOTE_CONFIG, build_config(), stat.S_IRUSR | stat.S_IWUSR)
    remote.write_bytes(REMOTE_SERVICE, build_service(), stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    remote.run(
        f"chown -R mihomo-cover:mihomo-cover {shlex.quote(REMOTE_CONFIG_DIR)} "
        f"{shlex.quote(REMOTE_STATE_DIR)}"
    )
    remote.run("systemctl daemon-reload", timeout=120)
    remote.run(f"{REMOTE_BINARY} -t -d {REMOTE_STATE_DIR} -f {REMOTE_CONFIG}", timeout=120)
    remote.run("systemctl enable mihomo-cover.service", timeout=120)
    remote.run("systemctl restart mihomo-cover.service", timeout=120)
    remote.remove_file(STALE_PROVIDER)


def verify_remote(remote: RemoteSession) -> dict[str, str]:
    commands = {
        "service": (
            "systemctl show mihomo-cover.service "
            "--property=UnitFileState,ActiveState,SubState,MainPID,MemoryCurrent,CPUUsageNSec --no-pager"
        ),
        "config_test": f"{REMOTE_BINARY} -t -d {REMOTE_STATE_DIR} -f {REMOTE_CONFIG}",
        "journal": "journalctl -u mihomo-cover.service --since '5 minutes ago' --no-pager",
        "provider_meta": (
            f"stat -c 'mode=%a owner=%U group=%G bytes=%s' {REMOTE_PROVIDER}; "
            f"sed -n '1p' {REMOTE_PROVIDER}; "
            f"if test -e {STALE_PROVIDER}; then echo stale_provider=present; "
            "else echo stale_provider=absent; fi"
        ),
        "public_dns": (
            "timeout 8 nslookup gzdata1.233netpro.com 223.5.5.5 2>&1 || "
            "timeout 8 getent ahostsv4 gzdata1.233netpro.com 2>&1 || true"
        ),
        "listener": "ss -lntp '( sport = :17890 )'",
        "youtube": (
            f"curl -fsSL --max-time 30 --proxy {PROXY_URL} -o /dev/null "
            "-w 'status=%{http_code} type=%{content_type} bytes=%{size_download} "
            "total=%{time_total}\\n' https://www.youtube.com/ 2>&1"
        ),
        "thumbnail": (
            f"curl -fsSL --max-time 30 --proxy {PROXY_URL} -o /dev/null "
            "-w 'status=%{http_code} type=%{content_type} bytes=%{size_download} "
            "total=%{time_total}\\n' "
            "https://i.ytimg.com/vi/d4kL7VHzkZA/hqdefault.jpg 2>&1"
        ),
        "api_health": "curl -fsS --max-time 10 http://127.0.0.1:18101/api/v1/health/live",
        "critical_services": (
            "for unit in nginx docker dramaloom 'contest-site@green' "
            "novel-similarity-api novel-similarity-qdrant; do "
            "printf '%s=' \"$unit\"; systemctl is-active \"$unit\" || true; done"
        ),
        "contest_slots": (
            "systemctl list-units 'contest-site@*' --all --no-pager --no-legend; "
            "systemctl status 'contest-site@green.service' --no-pager -l 2>&1 | tail -n 15"
        ),
        "containers": "docker ps --format '{{.Names}}={{.Status}}' 2>/dev/null || true",
        "network_invariants": (
            "ip route show; printf '\\nDNS\\n'; cat /etc/resolv.conf; "
            "printf '\\nGLOBAL_PROXY_ENV\\n'; "
            "systemctl show-environment | grep -iE '(^|_)https?_proxy=|all_proxy=|no_proxy=' || true"
        ),
        "firewall": "firewall-cmd --list-ports 2>/dev/null || true",
        "ssh_service": (
            "systemctl show sshd.service "
            "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp --no-pager; "
            "sshd -T 2>/dev/null | grep -E "
            "'^(clientaliveinterval|clientalivecountmax|maxsessions|maxstartups|tcpkeepalive) ' || true"
        ),
        "ssh_recent": (
            "journalctl -u sshd.service --since '90 minutes ago' --no-pager "
            "| grep -Ei 'disconnect|closed|timeout|error|fail|reset|broken|refused' "
            "| tail -n 80 || true"
        ),
        "resource_pressure": (
            "uptime; free -m; "
            "ps -C mihomo-cover -o pid,pcpu,pmem,rss,etime,cmd --no-headers 2>/dev/null || true; "
            "journalctl -k --since '90 minutes ago' --no-pager "
            "| grep -Ei 'out of memory|oom-killer|killed process|nf_conntrack.*full' "
            "| tail -n 30 || true"
        ),
    }
    return {name: remote.run(command, timeout=60, allow_failure=True).strip() for name, command in commands.items()}


def main() -> int:
    args = parse_args()
    host = _ascii_value(args.resource_file, 2)
    username = _ascii_value(args.resource_file, 3)
    password = _ascii_value(args.resource_file, 4)
    remote = RemoteSession(host, username, password)
    try:
        if args.inspect_only:
            print(json.dumps(inspect_remote(remote), ensure_ascii=False, indent=2))
        if args.install:
            install_remote(
                remote,
                read_subscription_from_stdin=args.subscription_stdin,
                subscription_proxy=args.subscription_proxy,
                provider_file=args.provider_file,
            )
        if args.verify:
            print(json.dumps(verify_remote(remote), ensure_ascii=False, indent=2))
    finally:
        remote.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
