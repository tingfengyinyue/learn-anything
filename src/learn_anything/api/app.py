from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import get_settings
from ..core.errors import StepError
from ..db import init_db
from ..llm import LLMError, get_llm
from ..search import SearchError, get_search
from ..sources.extract import ExtractError
from . import routes_auth, routes_learning, routes_topics


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db()
        yield

    app = FastAPI(title="learn-anything", version=__version__, lifespan=lifespan)

    def handler(status: int):
        async def _h(_: Request, exc: Exception):
            return JSONResponse(status_code=status, content={"detail": str(exc)})
        return _h

    app.add_exception_handler(StepError, handler(409))
    app.add_exception_handler(ExtractError, handler(400))
    app.add_exception_handler(LLMError, handler(502))
    app.add_exception_handler(SearchError, handler(502))

    @app.get("/health")
    def health():
        s = get_settings()
        try:
            llm = get_llm().name
        except LLMError as e:
            llm = f"未就绪：{e}"
        search = get_search()
        return {"status": "ok", "version": __version__, "llm": llm,
                "search": search.name if search else "none", "language": s.language,
                "allow_signup": s.allow_signup}

    app.include_router(routes_auth.router)
    app.include_router(routes_topics.router)
    app.include_router(routes_learning.router)
    return app

