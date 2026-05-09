import argparse
import json
import shlex
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib import error, request

import paramiko


DEFAULT_HOST = "192.168.97.154"
DEFAULT_USER = "a111"
DEFAULT_OLLAMA_URL = "http://192.168.97.154:11434"
DEFAULT_QDRANT_URL = "http://192.168.97.154:6333"
DEFAULT_MODEL = "qwen3-embedding:8b"
DEFAULT_REMOTE_ROOT_NAME = "novel_similarity_semantic_node"
DEFAULT_REMOTE_SUBDIRS = [
    "app",
    "scripts",
    "logs",
    "data",
    "cache",
    "config",
]
DEFAULT_COLLECTIONS = [
    "novel_chapter_embeddings",
    "novel_semantic_chunk_embeddings",
]


class SetupError(RuntimeError):
    pass


@dataclass
class CollectionStatus:
    name: str
    existed: bool
    vector_size: int


def http_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SetupError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise SetupError(f"Request failed for {url}: {exc}") from exc


def get_embedding_dimension(ollama_url: str, model: str) -> int:
    payload = {
        "model": model,
        "input": "这是用于检测 embedding 维度的初始化样本。",
    }
    resp = http_json("POST", f"{ollama_url}/api/embed", payload)
    embeddings = resp.get("embeddings")
    if not isinstance(embeddings, list) or not embeddings:
        raise SetupError(f"Unexpected Ollama response: {resp}")
    vector = embeddings[0]
    if not isinstance(vector, list) or not vector:
        raise SetupError(f"Unexpected embedding vector: {resp}")
    return len(vector)


def get_existing_collection(qdrant_url: str, name: str) -> Optional[Dict[str, Any]]:
    url = f"{qdrant_url}/collections/{name}"
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except error.HTTPError as exc:
        if exc.code == 404:
            return None
        body = exc.read().decode("utf-8", errors="replace")
        raise SetupError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise SetupError(f"Request failed for {url}: {exc}") from exc


def ensure_collection(qdrant_url: str, name: str, vector_size: int) -> CollectionStatus:
    existing = get_existing_collection(qdrant_url, name)
    if existing is not None:
        try:
            current_size = existing["result"]["config"]["params"]["vectors"]["size"]
        except KeyError as exc:
            raise SetupError(f"Unexpected collection payload for {name}: {existing}") from exc
        if current_size != vector_size:
            raise SetupError(
                f"Collection {name} already exists but vector size is {current_size}, expected {vector_size}."
            )
        return CollectionStatus(name=name, existed=True, vector_size=current_size)

    payload = {
        "vectors": {
            "size": vector_size,
            "distance": "Cosine",
        },
        "on_disk_payload": True,
    }
    http_json("PUT", f"{qdrant_url}/collections/{name}", payload)
    return CollectionStatus(name=name, existed=False, vector_size=vector_size)


def ssh_connect(host: str, user: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=22,
        username=user,
        password=password,
        timeout=10,
        banner_timeout=10,
        auth_timeout=10,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def ssh_run(client: paramiko.SSHClient, command: str) -> str:
    stdin, stdout, stderr = client.exec_command(command, timeout=30)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    code = stdout.channel.recv_exit_status()
    if code != 0:
        raise SetupError(f"Remote command failed ({code}): {command}\n{err}")
    return out


def ensure_remote_dirs(client: paramiko.SSHClient, remote_root_name: str, subdirs: List[str]) -> str:
    home_dir = ssh_run(client, "zsh -lc 'printf %s \"$HOME\"'")
    remote_root = f"{home_dir}/{remote_root_name}"
    paths = [remote_root] + [f"{remote_root}/{name}" for name in subdirs]
    quoted = " ".join(shlex.quote(path) for path in paths)
    ssh_run(client, f"zsh -lc 'mkdir -p {quoted}'")
    return remote_root


def write_remote_config(
    client: paramiko.SSHClient,
    remote_root: str,
    model: str,
    vector_size: int,
    collections: List[str],
) -> str:
    remote_path = f"{remote_root}/config/node_config.json"
    payload = {
        "node_role": "novel_similarity_semantic_node",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "embedding_model": model,
        "embedding_dimension": vector_size,
        "ollama_base_url": "http://127.0.0.1:11434",
        "qdrant_url": "http://127.0.0.1:6333",
        "collections": collections,
        "semantic_chunk_policy": {
            "window_chars": 800,
            "overlap_chars": 200,
        },
        "evidence_window_policy": {
            "window_chars": 200,
            "step_chars": 50,
        },
    }
    with client.open_sftp().file(remote_path, "w") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False, indent=2))
    return remote_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap the Mac semantic node.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", required=True)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--remote-root-name", default=DEFAULT_REMOTE_ROOT_NAME)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    vector_size = get_embedding_dimension(args.ollama_url, args.model)
    print(f"Embedding model: {args.model}")
    print(f"Embedding dimension: {vector_size}")

    collection_statuses = [
        ensure_collection(args.qdrant_url, name, vector_size) for name in DEFAULT_COLLECTIONS
    ]

    client = ssh_connect(args.host, args.user, args.password)
    try:
        remote_root = ensure_remote_dirs(client, args.remote_root_name, DEFAULT_REMOTE_SUBDIRS)
        remote_config = write_remote_config(
            client,
            remote_root=remote_root,
            model=args.model,
            vector_size=vector_size,
            collections=DEFAULT_COLLECTIONS,
        )
        listing = ssh_run(client, f"zsh -lc 'find {shlex.quote(remote_root)} -maxdepth 2 -type d | sort'")
    finally:
        client.close()

    print("")
    print("Collections:")
    for status in collection_statuses:
        state = "reused" if status.existed else "created"
        print(f"- {status.name}: {state} (dim={status.vector_size})")

    print("")
    print(f"Remote root: {remote_root}")
    print(f"Remote config: {remote_config}")
    print("Remote directories:")
    print(listing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
