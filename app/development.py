"""Historical real-data demos are closed unless the server explicitly opts into development."""

from fastapi import Request
from fastapi.responses import JSONResponse


def restrict_legacy(app, prefixes, enabled):
    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if not enabled and request.url.path.startswith(prefixes):
            return JSONResponse(
                status_code=403, content={"status": "use_authenticated_control_api"}
            )
        return await call_next(request)
