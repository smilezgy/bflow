"""Bflow —— 应用入口。

启动顺序：初始化数据目录 → 初始化数据库（建表+迁移）→ 挂载 API → 挂载前端静态资源。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bflow.api.response import ApiError, fail
from bflow.api.routes import router
from bflow.config import APP_VERSION, ROOT_DIR, settings
from bflow.db import init_db

log = logging.getLogger("bflow")


def create_app() -> FastAPI:
    settings.ensure_dirs()
    init_db()

    app = FastAPI(title="Bflow", version=APP_VERSION,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.exception_handler(ApiError)
    async def on_api_error(request: Request, exc: ApiError):
        return fail(exc.code, exc.message, status=exc.status, hint=exc.hint,
                    retryable=exc.retryable, detail=exc.detail)

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception):
        log.exception("未处理异常: %s", exc)
        return fail("CHANNEL_INTERNAL_ERROR", str(exc), status=500,
                    hint="这是服务端异常，请查看日志。", retryable=True)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "data": {"status": "healthy", "version": APP_VERSION},
                "meta": {"request_id": "health"}}

    # 前端静态资源
    static_dir = ROOT_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/")
        async def index():
            return FileResponse(static_dir / "index.html")

    return app


app = create_app()


def main() -> None:
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    print(f"Bflow {APP_VERSION} → http://{settings.host}:{settings.port}")
    print(f"数据目录: {settings.data_root}")
    uvicorn.run(app, host=settings.host, port=settings.port,
                ws_ping_interval=None, ws_ping_timeout=None)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT_DIR))
    main()
