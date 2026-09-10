"""
Encrypted DB backup worker.

Pipeline:
  1. pg_dump → compressed SQL
  2. AES-256-GCM encrypt with smm:backup:v1 AAD
  3. SHA-256(ciphertext) for integrity verification
  4. Upload to S3-compatible storage
  5. Metadata-only notification (no data, no keys)
"""
from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

_BACKUP_AAD = b"smm:backup:v1"


@dataclass
class BackupResult:
    filename:      str
    size_bytes:    int
    sha256_hex:    str
    s3_key:        str
    encrypted:     bool
    completed_at:  datetime

    def to_notification_dict(self) -> dict:
        """Return metadata-only dict — no data, no keys."""
        return {
            "filename":     self.filename,
            "size_bytes":   self.size_bytes,
            "sha256_hex":   self.sha256_hex[:16] + "...",   # truncated — not full hash
            "s3_key":       self.s3_key,
            "encrypted":    self.encrypted,
            "completed_at": self.completed_at.isoformat(),
        }


class BackupWorker:
    async def run(self) -> BackupResult:
        from app.core.config import settings
        ts       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"smm_backup_{ts}.sql.gz.enc"

        with tempfile.TemporaryDirectory() as tmpdir:
            dump_path = os.path.join(tmpdir, "dump.sql.gz")
            enc_path  = os.path.join(tmpdir, filename)

            # Step 1: pg_dump
            await self._pg_dump(settings.database_url_sync, dump_path)

            # Step 2: AES-256-GCM encrypt
            sha256_hex = self._encrypt_file(dump_path, enc_path, settings.backup_encryption_key)

            # Step 3: Upload
            size = os.path.getsize(enc_path)
            s3_key = f"backups/{ts[:8]}/{filename}"
            await self._upload_s3(enc_path, s3_key, settings)

        return BackupResult(
            filename=filename, size_bytes=size,
            sha256_hex=sha256_hex, s3_key=s3_key,
            encrypted=True, completed_at=datetime.now(timezone.utc),
        )

    @staticmethod
    async def _pg_dump(db_url: str, out_path: str) -> None:
        import asyncio
        pg_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        proc = await asyncio.create_subprocess_exec(
            "pg_dump", "--format=custom", "--compress=9", "-f", out_path, pg_url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {stderr.decode()}")

    @staticmethod
    def _encrypt_file(src: str, dst: str, hex_key: str) -> str:
        """AES-256-GCM encrypt, return SHA-256(ciphertext) hex."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key   = bytes.fromhex(hex_key)
        nonce = secrets.token_bytes(12)

        with open(src, "rb") as f:
            plaintext = f.read()

        ciphertext = AESGCM(key).encrypt(nonce, plaintext, _BACKUP_AAD)
        payload    = nonce + ciphertext
        sha256     = hashlib.sha256(payload).hexdigest()

        with open(dst, "wb") as f:
            f.write(payload)

        return sha256

    @staticmethod
    async def _upload_s3(local_path: str, s3_key: str, settings) -> None:
        try:
            import aioboto3  # type: ignore
            session = aioboto3.Session()
            async with session.client(
                "s3",
                endpoint_url=settings.backup_s3_endpoint or None,
                aws_access_key_id=settings.backup_s3_access_key,
                aws_secret_access_key=settings.backup_s3_secret_key,
            ) as client:
                await client.upload_file(local_path, settings.backup_s3_bucket, s3_key)
        except ImportError:
            logger.warning("aioboto3_not_installed_skipping_s3_upload")
        except Exception as exc:
            logger.error("backup_s3_upload_failed", error=str(exc))
            raise
