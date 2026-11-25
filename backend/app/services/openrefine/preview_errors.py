"""Structured OpenRefine API errors."""
from fastapi import HTTPException


def http_error(status_code: int, code: str, message: str, **extra) -> HTTPException:
    detail = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)
