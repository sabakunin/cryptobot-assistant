import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response

from .web_login import LoginError, validate_init_data

WEB = Path(__file__).with_name("web")
SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors https://web.telegram.org https://*.telegram.org"
    ),
}


def add_login_routes(app, config, runtime):
    @app.middleware("http")
    async def login_headers(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/connect"):
            response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/connect")
    async def login_page():
        return Response((WEB / "connect.html").read_bytes(), media_type="text/html")

    @app.get("/connect/{asset}")
    async def login_asset(asset: str):
        types = {"connect.js": "application/javascript", "connect.css": "text/css"}
        if asset not in types:
            raise HTTPException(404)
        return Response((WEB / asset).read_bytes(), media_type=types[asset])

    @app.post("/connect/api/{action}")
    async def login_action(action: str, request: Request):
        if not config.connect_url or not runtime.ready or not getattr(runtime, "login", None):
            raise HTTPException(503, "Вход ещё не настроен на хостинге.")
        origin = request.headers.get("origin")
        public = urlsplit(config.connect_url)
        if origin and origin != f"{public.scheme}://{public.netloc}":
            raise HTTPException(403, "Недопустимый источник запроса.")
        try:
            user_id = validate_init_data(
                request.headers.get("x-telegram-init-data", ""),
                config.bot_token.get_secret_value(),
                None,
            )
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(401, "Открой вход кнопкой в своём управляющем боте.") from None
        try:
            login = await runtime.login_for(user_id)
        except LoginError as error:
            raise HTTPException(error.status, error.message) from None
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8192:
                raise HTTPException(413, "Слишком большой запрос.")
        try:
            body = json.loads(data or b"{}")
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Неверный запрос.") from None
        attempt_id = body.get("id", "")
        if not isinstance(attempt_id, str) or len(attempt_id) > 100:
            raise HTTPException(400, "Неверный запрос.")
        try:
            if action == "start":
                return await login.start()
            if action == "status":
                return login.current(attempt_id)
            if action == "password":
                password = body.pop("password", None)
                data.clear()
                try:
                    return await login.password(attempt_id, password)
                finally:
                    password = None
            if action == "cancel":
                await login.cancel(attempt_id)
                return {"ok": True}
            raise HTTPException(404)
        except LoginError as error:
            raise HTTPException(error.status, error.message) from None
