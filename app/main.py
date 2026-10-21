import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from app.auth import bootstrap_admin
from app.http import install
from openapi_server.apis.auth_api import router as auth_router
from openapi_server.apis.products_api import router as products_router
from openapi_server.apis.orders_api import router as orders_router
from openapi_server.apis.promos_api import router as promos_router

logging.basicConfig(level=logging.INFO, format="%(message)s")


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(bootstrap_admin)
    yield


app = FastAPI(title="Marketplace HW2", lifespan=lifespan, redoc_url=None)
install(app)
for router in (auth_router, products_router, orders_router, promos_router):
    app.include_router(router)


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


def contract():
    return json.loads((Path(__file__).resolve().parents[1] / "openapi/marketplace.json").read_text("utf-8"))


app.openapi = contract
