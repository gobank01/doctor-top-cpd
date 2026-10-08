"""Private Vercel Blob storage for the Doctor Top CPD shared studio.

All callers use project-relative object names.  Tokens remain on the server;
media is served by the application, never by raw public Blob URLs. Completed
voices and audio are shared within this no-password studio; pending clips are
scoped by anonymous browser sessions in the application.
The interface deliberately does not treat process-local files as durable state.
"""
from __future__ import annotations

import json
import os
from typing import Any


PREFIX = "doctor-top-cpd/"


def validate_path(path: str, *, prefix: bool = False) -> str:
    """Accept only literal relative paths, preserving Thai filenames exactly."""
    if not isinstance(path, str):
        raise ValueError("Object path must be a string")
    if not path and prefix:
        return ""
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError("Invalid relative object path")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        raise ValueError("Control characters are not allowed in object paths")
    parts = path[:-1].split("/") if prefix and path.endswith("/") else path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Object path contains an invalid segment")
    return path


class CloudStore:
    """Small synchronous adapter around ``vercel==0.11.5`` BlobClient."""

    def __init__(self, token: str | None = None, *, client: Any = None):
        if client is not None:
            self.client = client
            return
        resolved_token = (token or os.environ.get("BLOB_READ_WRITE_TOKEN", "")).strip()
        if not resolved_token:
            raise RuntimeError("BLOB_READ_WRITE_TOKEN is not configured")
        from vercel.blob import BlobClient

        self.client = BlobClient(token=resolved_token)

    def get_bytes(self, path: str) -> bytes | None:
        from vercel.blob.errors import BlobNotFoundError

        key = PREFIX + validate_path(path)
        try:
            result = self.client.get(key, access="private", use_cache=False)
        except BlobNotFoundError:
            return None
        if result is None:
            return None
        if result.status_code != 200:
            raise RuntimeError("Unexpected response while reading a private object")
        # The pinned Python SDK returns buffered content, not the JavaScript
        # SDK's stream/blob object shape. HTTP routes must stream their response.
        return bytes(result.content)

    def put_bytes(self, path: str, data: bytes,
                  content_type: str = "application/octet-stream", overwrite: bool = True) -> None:
        self.client.put(
            PREFIX + validate_path(path), bytes(data), access="private",
            content_type=content_type, add_random_suffix=False, overwrite=overwrite,
            cache_control_max_age=60,
        )

    def delete(self, path: str) -> None:
        self.client.delete(PREFIX + validate_path(path))

    def list_paths(self, prefix: str) -> list[str]:
        scoped_prefix = PREFIX + validate_path(prefix, prefix=True)
        cursor = None
        seen_cursors = set()
        paths: set[str] = set()
        while True:
            page = self.client.list_objects(prefix=scoped_prefix, cursor=cursor, limit=1000)
            for blob in page.blobs:
                if not blob.pathname.startswith(scoped_prefix):
                    raise RuntimeError("Blob listing returned an object outside the requested prefix")
                relative = blob.pathname[len(PREFIX):]
                validate_path(relative)
                paths.add(relative)
            if not page.has_more:
                break
            if not page.cursor or page.cursor in seen_cursors:
                raise RuntimeError("Blob pagination did not advance")
            cursor = page.cursor
            seen_cursors.add(cursor)
        return sorted(paths)

    def get_json(self, path: str, default: Any = None) -> Any:
        data = self.get_bytes(path)
        return default if data is None else json.loads(data.decode("utf-8"))

    def put_json(self, path: str, obj: Any) -> None:
        encoded = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        self.put_bytes(path, encoded, "application/json; charset=utf-8")

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
