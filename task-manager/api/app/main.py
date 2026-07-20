from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.core.config import settings
from app.core.database import init_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print(f"🚀 Starting {settings.APP_NAME}...")
    await init_db()
    yield
    # Shutdown
    print(f"🛑 Shutting down {settings.APP_NAME}...")

app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    lifespan=lifespan
)

# CORS для виджета (в проде ограничь до https://vpk-oil.ru)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "db": "sqlite",
        "cache": "redis"
    }

# TODO: Подключение роутеров
# app.include_router(tasks_router, prefix="/api/v1/tasks")
# app.include_router(users_router, prefix="/api/v1/users")