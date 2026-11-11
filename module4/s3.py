"""The durable mirror for Module 4's archive.

S3 is object storage, not a filesystem. Module 4 keeps its local
``data/history/history.json`` as the fast read path and mirrors that one archive
object under a configured prefix. The local file is what the dashboard reads;
the object is what survives the VM.

Credentials are never part of the framework. ``boto3`` reads the normal AWS
environment, profile, or ``AWS_SHARED_CREDENTIALS_FILE`` supplied by the host.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.exceptions import ClientError

from module4.store import archive_payload


class S3ArchiveError(RuntimeError):
    """The remote archive could not be read or written."""


class S3Archive:
    """One JSON archive object under a private S3 prefix."""

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "stock market v3",
        region: str | None = None,
        client: Any = None,
    ) -> None:
        self.bucket = str(bucket).strip()
        if not self.bucket:
            raise ValueError("an S3 archive needs a bucket")
        self.prefix = "/".join(part for part in str(prefix).strip("/").split("/") if part)
        self.region = region or None
        self.key = f"{self.prefix}/history.json" if self.prefix else "history.json"
        self._client = client or boto3.client("s3", region_name=self.region)

    def load(self) -> dict[str, Any] | None:
        """Read the remote archive, or ``None`` when it does not exist yet."""

        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self.key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            if code in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise S3ArchiveError(
                f"could not read s3://{self.bucket}/{self.key}: {code or type(exc).__name__}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - the caller records the failure
            raise S3ArchiveError(
                f"could not read s3://{self.bucket}/{self.key}: {type(exc).__name__}"
            ) from exc
        body = response.get("Body")
        if body is None:
            raise S3ArchiveError("S3 returned no body")
        raw = body.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise S3ArchiveError(
                f"s3://{self.bucket}/{self.key} is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise S3ArchiveError(
                f"s3://{self.bucket}/{self.key} is not an archive object"
            )
        return payload

    def save(self, orders: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the remote archive atomically from S3's point of view."""

        payload = archive_payload(orders)
        body = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=body,
                ContentType="application/json",
                ServerSideEncryption="AES256",
            )
        except Exception as exc:  # noqa: BLE001 - reported through the ledger
            raise S3ArchiveError(
                f"could not write s3://{self.bucket}/{self.key}: {type(exc).__name__}"
            ) from exc

    def describe(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "prefix": self.prefix,
            "key": self.key,
            "region": self.region,
        }
