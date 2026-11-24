#!/usr/bin/env python3
"""Create a private .env with fresh credentials for a local Compose install."""

import base64
import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".env"
TEMPLATE = ROOT / ".env.example"


def main() -> None:
    if TARGET.exists():
        raise SystemExit(".env already exists; move it aside before generating new credentials")
    values = {
        "JWT_SECRET_KEY": secrets.token_hex(32),
        "APP_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        "INIT_ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "MINIO_SECRET_KEY": secrets.token_hex(24),
    }
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines()
    # Replace only known credential lines, preserving all other documented settings.
    rendered = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        rendered.append(f"{key}={values[key]}" if key in values else line)
    TARGET.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    TARGET.chmod(0o600)
    print("Created .env with fresh credentials (file mode 0600).")
    print(f"Initial admin email: {next(line.split('=', 1)[1] for line in rendered if line.startswith('INIT_ADMIN_EMAIL='))}")
    print(f"Initial admin password (save it now): {values['INIT_ADMIN_PASSWORD']}")


if __name__ == "__main__":
    main()
