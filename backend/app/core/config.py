"""环境配置中心（支持 Docker 生产模式与本地轻量测试模式）"""

import os
from dataclasses import dataclass


@dataclass
class Settings:
    app_name: str = os.getenv("APP_NAME", "零点未来 · 企业语义统一平台")
    app_env: str = os.getenv("APP_ENV", "development")

    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "lingdianweilai-su-jwt-secret-2026")
    jwt_expire_minutes: int = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))
    app_encryption_key: str = os.getenv("APP_ENCRYPTION_KEY", "")

    init_admin_email: str = os.getenv("INIT_ADMIN_EMAIL", "admin@lingdianweilai.ai")
    init_admin_password: str = os.getenv("INIT_ADMIN_PASSWORD", "Admin@2026")
    init_admin_name: str = os.getenv("INIT_ADMIN_NAME", "系统管理员")
    init_workspace_name: str = os.getenv("INIT_WORKSPACE_NAME", "零点未来默认工作空间")
    allow_public_signup: bool = os.getenv("ALLOW_PUBLIC_SIGNUP", "false").lower() == "true"

    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./local_dev.db")
    redis_url: str = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    celery_enabled: bool = os.getenv("CELERY_ENABLED", "false").lower() == "true"
    phoenix_enabled: bool = os.getenv("PHOENIX_ENABLED", "false").lower() == "true"
    phoenix_collector_endpoint: str = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006/v1/traces")
    otel_service_name: str = os.getenv("OTEL_SERVICE_NAME", "open-semantic-unifier")
    otel_export_timeout_seconds: float = float(os.getenv("OTEL_EXPORT_TIMEOUT_SECONDS", "1.0"))
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "30.0"))
    unification_max_attempts: int = int(os.getenv("UNIFICATION_MAX_ATTEMPTS", "3"))

    storage_backend: str = os.getenv("STORAGE_BACKEND", "local")  # local | s3
    local_storage_dir: str = os.getenv("LOCAL_STORAGE_DIR", "./local_storage")
    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    s3_access_key: str = os.getenv("S3_ACCESS_KEY", "minioadmin")
    s3_secret_key: str = os.getenv("S3_SECRET_KEY", "minioadmin123")
    s3_bucket: str = os.getenv("S3_BUCKET", "semantic-unifier")

    def validate_for_deployment(self) -> None:
        """Reject invalid retry limits and the repository's example credentials in production mode."""
        if self.unification_max_attempts < 1:
            raise ValueError("UNIFICATION_MAX_ATTEMPTS must be at least 1")
        if self.app_env.lower() != "production":
            return
        required_secrets = {
            "JWT_SECRET_KEY": self.jwt_secret_key,
            "INIT_ADMIN_PASSWORD": self.init_admin_password,
            "S3_SECRET_KEY": self.s3_secret_key,
        }
        invalid = [
            name
            for name, value in required_secrets.items()
            if len(value.strip()) < 24 or value.strip().startswith("generate-with-")
        ]
        if invalid:
            raise ValueError(
                "Production credentials are missing or too short: " + ", ".join(invalid)
                + ". Generate a private .env with scripts/init_env.py."
            )


settings = Settings()
