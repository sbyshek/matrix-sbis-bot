import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

from app.core import api, dashboard, desk, location
from app.core.background import auto_close_checker
from app.core.redis import init_locations_state, init_redis
from app.security import LOCATIONS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_redis()
    await init_locations_state(list(LOCATIONS.keys()))

    auto_close_task = asyncio.create_task(auto_close_checker())

    logger.info("✅ Incident Dispatcher started")

    yield

    # Shutdown
    auto_close_task.cancel()

    try:
        await auto_close_task
    except asyncio.CancelledError:
        pass

    logger.info("🛑 Incident Dispatcher stopped")


app = FastAPI(
    title="Matrix Incident Dispatcher",
    lifespan=lifespan
)

app.include_router(dashboard.router)
app.include_router(location.router)
app.include_router(api.router)
app.include_router(desk.router)


@app.exception_handler(404)
async def custom_404(request: Request, exc):
    return templates.TemplateResponse(
        "404.html",
        {
            "request": request,
            "message": "Запрашиваемый ресурс не найден"
        },
        status_code=404
    )