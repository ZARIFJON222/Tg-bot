import os
import sys
import logging
import asyncio

# Ensure project root is in sys.path
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from fastapi import FastAPI, Request, Response, status
from aiogram.types import Update
from main import bot, dp, sdb, init_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vercel_webhook")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

app = FastAPI(title="Telegram Bot on Vercel")


@app.on_event("startup")
async def on_startup():
    await init_app()


@app.get("/")
@app.get("/api")
async def health_check():
    if not sdb._initialized:
        await sdb.load_all()
    return {
        "status": "online",
        "service": "Telegram Bot on Vercel",
        "channels": len(sdb.channels),
        "movies": len(sdb.movies),
        "users": len(sdb.users),
        "database": "Supabase PostgreSQL",
    }


@app.post("/api/webhook")
@app.post("/webhook")
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        secret_header = request.headers.get("x-telegram-bot-api-secret-token")
        if secret_header != WEBHOOK_SECRET:
            logger.warning("Rejected unauthorized webhook request: invalid secret token")
            return Response(content="Unauthorized", status_code=status.HTTP_403_FORBIDDEN)

    if not sdb._initialized:
        await sdb.load_all()

    try:
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
        return Response(content="ok", media_type="text/plain", status_code=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"Error handling Telegram webhook update: {e}", exc_info=True)
        # Always return 200 OK so Telegram doesn't retry repeatedly
        return Response(content="error", media_type="text/plain", status_code=status.HTTP_200_OK)
