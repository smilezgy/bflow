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
from starlette.responses import Response

from bflow.api.response import ApiError, fail
from bflow.api.routes import router
from bflow.config import APP_VERSION, ROOT_DIR, settings
from bflow.db import init_db

log = logging.getLogger("bflow")

# 静态资源的缓存策略：必须每次回源校验。
#
# 背景：StaticFiles 默认不发 Cache-Control，浏览器只能按「距上次修改时间
# 的 10%」这种启发式规则自己猜缓存时长。结果是改了前端代码、刷新页面，
# 浏览器仍用磁盘缓存里的旧文件 —— 界面看起来正常，但跑的是旧逻辑。
#
# 这里显式发 no-cache：浏览器每次都带条件请求回服务端，服务端按 ETag 比对，
# 内容没变就回 304（不重复传输），变了就回新文件。本地应用资源体积小、
# 读取零成本，这个取舍没有代价，但能根治「改了不生效」。
NO_CACHE = "no-cache, must-revalidate"


class NoCacheStaticFiles(StaticFiles):
    """给所有静态资源响应加上 Cache-Control: no-cache。"""

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = NO_CACHE
        return resp


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
        app.mount("/static", NoCacheStaticFiles(directory=str(static_dir)),
                  name="static")

        @app.get("/")
        async def index():
            resp = FileResponse(static_dir / "index.html")
            resp.headers["Cache-Control"] = NO_CACHE
            return resp

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon():
            icon = static_dir / "favicon.svg"
            if icon.exists():
                resp = FileResponse(icon, media_type="image/svg+xml")
                resp.headers["Cache-Control"] = NO_CACHE
                return resp
            return Response(status_code=204)

    return app


app = create_app()


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(prog="bflow", description="Bflow —— AI 多模态创作工作台")
    parser.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8000）")
    parser.add_argument("--data-root", default=None, help="数据目录")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重载")
    args = parser.parse_args()

    if args.data_root:
        settings.set_data_root(args.data_root)
    host = args.host or settings.host
    port = args.port or settings.port

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    print(f"Bflow {APP_VERSION} → http://{host}:{port}/")
    print(f"数据目录: {settings.data_root}")
    uvicorn.run(app, host=host, port=port, reload=args.reload,
                ws_ping_interval=None, ws_ping_timeout=None)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT_DIR))
    main()
