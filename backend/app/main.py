"""零点未来 · 企业语义统一平台（Semantic Unifier Platform）FastAPI 入口"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import auth_team, projects_sources, providers_jobs, reviews_serving, unification
from app.core.config import settings
from app.core.db import init_db_and_seed


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.validate_for_deployment()
    init_db_and_seed()
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_team.router)
app.include_router(projects_sources.router)
app.include_router(providers_jobs.router)
app.include_router(reviews_serving.router)
app.include_router(unification.router)


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name, "env": settings.app_env}
