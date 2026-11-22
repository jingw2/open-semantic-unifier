"""统一对象存储抽象：生产对接 MinIO (S3)，本地开发/测试无缝切换本地磁盘目录"""

import os
from typing import Tuple

from app.core.config import settings


class StorageService:
    def __init__(self):
        self.backend = settings.storage_backend
        self.local_root = os.path.abspath(settings.local_storage_dir)
        os.makedirs(self.local_root, exist_ok=True)
        self._s3 = None

    def _get_s3(self):
        if self._s3 is None:
            import boto3
            from botocore.exceptions import ClientError

            self._s3 = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
            )
            try:
                self._s3.head_bucket(Bucket=settings.s3_bucket)
            except ClientError:
                self._s3.create_bucket(Bucket=settings.s3_bucket)
        return self._s3

    def put_bytes(self, object_key: str, data: bytes) -> Tuple[str, int]:
        clean_key = object_key.lstrip("/")
        if self.backend == "s3":
            s3 = self._get_s3()
            s3.put_object(Bucket=settings.s3_bucket, Key=clean_key, Body=data)
            return clean_key, len(data)
        dest = os.path.join(self.local_root, clean_key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        return clean_key, len(data)

    def put_file(self, object_key: str, local_path: str) -> Tuple[str, int]:
        with open(local_path, "rb") as f:
            return self.put_bytes(object_key, f.read())

    def get_bytes(self, object_key: str) -> bytes:
        clean_key = object_key.lstrip("/")
        if self.backend == "s3":
            s3 = self._get_s3()
            return s3.get_object(Bucket=settings.s3_bucket, Key=clean_key)["Body"].read()
        src = os.path.join(self.local_root, clean_key)
        with open(src, "rb") as f:
            return f.read()

    def materialize_to_local(self, object_key: str, target_path: str) -> str:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        data = self.get_bytes(object_key)
        with open(target_path, "wb") as f:
            f.write(data)
        return target_path


storage = StorageService()
