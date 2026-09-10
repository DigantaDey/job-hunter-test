"""
Public tracking endpoints (no auth — they are embedded in outbound email).

* open pixel  — records an open (note: mail clients proxy images, so opens are a
  signal, not proof of reading — stated in the UI);
* one-click unsubscribe (RFC 8058 style POST + a GET landing page);
* provider webhooks for bounce/complaint events.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from app.core.config import settings
from app.core.logging import get_logger
from app.db import get_db
from app.services.outreach import handle_unsubscribe, record_open, record_webhook_event

router = APIRouter(prefix="/track", tags=["tracking"])
log = get_logger("app.tracking")

# 1x1 transparent GIF
_PIXEL = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unsubscribed — {app}</title>
<style>body{{font-family:system-ui,Segoe UI,sans-serif;margin:0;display:grid;place-items:center;min-height:100vh;background:#f4f4f5;color:#18181b}}
.card{{background:#fff;border:1px solid #e4e4e7;border-radius:16px;padding:32px;max-width:420px;text-align:center}}
h1{{font-size:18px;margin:0 0 8px}}p{{font-size:14px;color:#52525b;margin:0}}</style></head>
<body><div class="card"><h1>{heading}</h1><p>{body}</p></div></body></html>"""


@router.get("/open/{token}.png", include_in_schema=False)
def open_pixel(token: str):
    db = next(get_db())
    try:
        record_open(db, token)
    finally:
        db.close()
    return Response(content=_PIXEL, media_type="image/gif",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str):
    db = next(get_db())
    try:
        row = handle_unsubscribe(db, token)
    finally:
        db.close()
    if row is None:
        return HTMLResponse(_PAGE.format(app=settings.app_name, heading="Link expired",
                                         body="This unsubscribe link is no longer valid."), status_code=404)
    return HTMLResponse(_PAGE.format(app=settings.app_name, heading="You're unsubscribed",
                                     body=f"{row.email} will not receive further outreach from this account."))


@router.post("/unsubscribe/{token}", include_in_schema=False)
def unsubscribe_one_click(token: str):
    db = next(get_db())
    try:
        row = handle_unsubscribe(db, token)
    finally:
        db.close()
    if row is None:
        raise HTTPException(404, "Unknown unsubscribe token")
    return {"ok": True, "email": row.email}


@router.post("/events")
def provider_webhook(request: Request, payload: dict, x_webhook_token: str = Header(default="")):
    """Webhook sink for an email provider (bounce/complaint → suppression)."""
    if settings.email_webhook_token and x_webhook_token != settings.email_webhook_token:
        raise HTTPException(401, "Invalid webhook token")
    db = next(get_db())
    try:
        return record_webhook_event(db, x_webhook_token, payload or {})
    finally:
        db.close()
