import secrets
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from .config import Config
from .login_routes import add_login_routes
from .runtime import Runtime


def create_app(config: Config, runtime=None):
    runtime = runtime or Runtime(config)

    @asynccontextmanager
    async def lifespan(app):
        try:
            await runtime.start()
            yield
        finally:
            await runtime.close()

    app = FastAPI(
        title="CryptoBot Assistant", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.runtime = runtime
    add_login_routes(app, config, runtime)

    def authenticate(request, header="authorization"):
        wanted = config.webhook_secret.get_secret_value()
        received = request.headers.get(header, "")
        if header == "authorization":
            wanted = "Bearer " + wanted
        if not secrets.compare_digest(received, wanted):
            raise HTTPException(401, "Unauthorized")

    @app.get("/healthz")
    async def health():
        # No DB access from hosting probes, and no secret/account metadata.
        return Response("ok" if runtime.ready else "starting", status_code=200 if runtime.ready else 503)

    @app.get("/")
    async def root():
        return {"service": "CryptoBot Assistant", "version": "0.3.3", "control": "Telegram bot"}

    @app.post("/telegram/update")
    async def telegram_update(request: Request):
        authenticate(request, "x-telegram-bot-api-secret-token")
        if not runtime.ready:
            raise HTTPException(503, "Starting")
        raw = bytearray()
        async for piece in request.stream():
            raw.extend(piece)
            if len(raw) > 128000:
                raise HTTPException(413, "Update too large")
        try:
            update = Update.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(400, "Invalid update") from None
        await runtime.accept(update.model_dump(mode="json", exclude_none=True, by_alias=True))
        return {"ok": True}

    @app.get("/api/status")
    async def status(request: Request):
        authenticate(request)
        state = await runtime.store.read()
        return {
            "runtime": runtime.status,
            "running": state["settings"]["running"],
            "mode": state["settings"]["mode"],
            "active": bool(state["active"]),
            "stats": state["stats"],
        }

    @app.get("/metrics")
    async def metrics(request: Request):
        authenticate(request)
        return Response(generate_latest(runtime.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    return app
