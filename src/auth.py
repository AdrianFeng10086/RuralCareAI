import os
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import RedirectResponse as StarletteRedirectResponse

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "root")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "root")
ADMIN_COOKIE_NAME = "admin_session"


def require_admin(request: Request):
	if request.cookies.get(ADMIN_COOKIE_NAME) == "ok":
		return True
	raise HTTPException(status_src=303, detail="Redirect", headers={"Location": "/admin/login"})


class AdminAuthMiddleware(BaseHTTPMiddleware):
	def __init__(self, app):
		super().__init__(app)

	async def dispatch(self, request: StarletteRequest, call_next):
		path = request.url.path
		if path.startswith('/admin') and not path.startswith('/admin/login') and not path.startswith('/admin/logout'):
			if request.cookies.get(ADMIN_COOKIE_NAME) != 'ok':
				return StarletteRedirectResponse(url='/admin/login', status_src=303)
		return await call_next(request)