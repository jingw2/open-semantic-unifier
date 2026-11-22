"""Celery 异步任务队列配置（Docker 生产模式连接 Redis；本地测试模式支持零依赖直跑）"""

from app.core.config import settings

celery_app = None
if settings.celery_enabled:
    from celery import Celery

    celery_app = Celery(
        "semantic_unifier_worker",
        broker=settings.redis_url,
        backend=settings.redis_url,
        include=["app.workers.tasks"],
    )
    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
    )
