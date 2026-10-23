"""The admin page and API on their own port (D4).

With ADMIN_PORT set, the admin routes are NOT mounted on the main app. They run on a second listener
inside this same process (so they share the channel manager and settings). A tunnel or reverse proxy that
exposes the main port (webhooks, /chat, /health) therefore exposes no admin route at all.

One server process only: with several workers each would try to bind the admin port.
"""
import logging
import threading
import time

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.admin.alerts_api import router as alerts_router
from app.admin.api import router as admin_router
from app.admin.conversations_api import router as conversations_router
from app.admin.departments_api import router as departments_router
from app.admin.documents_api import router as documents_router
from app.admin.groups_api import router as groups_router
from app.admin.guardrails_api import router as guardrails_router
from app.admin.kbs_api import router as kbs_router
from app.admin.prompts_api import router as prompts_router
from app.admin.providers_api import router as providers_router
from app.admin.ui import router as ui_router
from app.admin.reports_api import router as reports_router
from app.admin.usage_api import router as usage_router
from app.admin.users_api import router as users_router
from app.admin.profile_api import router as profile_router
from app.admin.sso_api import router as sso_router
from app.config import settings

log = logging.getLogger(__name__)
ROUTERS = (admin_router, providers_router, users_router, departments_router, kbs_router, documents_router, groups_router, prompts_router, guardrails_router, conversations_router, usage_router, alerts_router, reports_router, profile_router, sso_router, ui_router)


def include_admin(app: FastAPI) -> None:
    for r in ROUTERS:
        app.include_router(r)


def build_app() -> FastAPI:
    app = FastAPI(title="Channel Agnostic Agent admin", docs_url=None, redoc_url=None, openapi_url=None)
    include_admin(app)

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/admin")

    return app


class AdminListener:
    def __init__(self):
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        host, port = settings.admin_host, settings.admin_port
        if settings.admin_allow_remote and settings.admin_require_https:
            log.warning("Remote admin is on: put an HTTPS reverse proxy in front of %s:%s (requests without HTTPS are refused).",
                        host, port)
        elif settings.admin_allow_remote:
            log.warning("Remote admin is on WITHOUT the HTTPS requirement (ADMIN_REQUIRE_HTTPS=false). "
                        "Publish %s:%s on this machine only.", host, port)
        config = uvicorn.Config(build_app(), host=host, port=port, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None      # the main server owns signals
        self._thread = threading.Thread(target=self._run, name="admin-listener", daemon=True)
        self._thread.start()
        for _ in range(50):                                      # wait up to 5 s for the port to open
            if self._server.started:
                log.info("Admin page listening on http://%s:%s/admin", host, port)
                return True
            if not self._thread.is_alive():
                break
            time.sleep(0.1)
        log.error("The admin listener could not start on %s:%s (port in use?). The admin page is unavailable.", host, port)
        return False

    def _run(self) -> None:
        try:
            self._server.run()
        except (SystemExit, Exception):
            log.exception("admin listener stopped")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


listener = AdminListener()
