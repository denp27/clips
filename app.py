import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Request, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    CallbackQuery,
)

import database as db
from telegram_auth import validate_init_data, InitDataError

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
WEBAPP_URL = os.environ.get("WEBAPP_URL", "http://localhost:8000")
PORT = int(os.environ.get("PORT", "8000"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

CATEGORY_LABELS = {
    "streamers": "Стримеры",
    "youtubers": "Ютуберы",
    "brands": "Бренды",
}

# ---------------------------------------------------------------------------
# Telegram bot handlers
# ---------------------------------------------------------------------------


@dp.message(CommandStart())
async def cmd_start(message: Message):
    db.get_or_create_user(
        tg_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Открыть Dani Clips", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )
    is_admin = message.from_user.id in ADMIN_IDS
    text = (
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Добро пожаловать в <b>Dani Clips</b> — платформу для заработка на клипах блогеров!\n\n"
        "✂️ Создавай нарезки популярных стримеров и ютуберов\n"
        "💰 Получай оплату за просмотры\n"
        "🚀 Выводи заработанное на карту\n"
        "👤 Приводи друзей и получай 10% с заработанных денег с нарезок твоих друзей\n\n"
        "Нажми кнопку ниже, чтобы начать!"
    )
    if is_admin:
        text += "\n\n🛠 У тебя есть доступ к админ-панели внутри мини-аппа."
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("wd_approve:") | F.data.startswith("wd_reject:"))
async def handle_withdrawal_decision(call: CallbackQuery):
    """Админ нажимает Одобрить/Отклонить прямо под заявкой в ЛС с ботом."""
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Недоступно", show_alert=True)
        return

    action, wd_id_str = call.data.split(":")
    wd_id = int(wd_id_str)
    wd = db.get_withdrawal(wd_id)
    if wd is None:
        await call.answer("Заявка не найдена", show_alert=True)
        return
    if wd["status"] != "pending":
        await call.answer("Заявка уже обработана", show_alert=True)
        return

    if action == "wd_approve":
        db.set_withdrawal_status(wd_id, "approved")
        db.adjust_balance(wd["uid"], -wd["amount"])
        await bot.send_message(
            wd["user_tg_id"],
            f"✅ Ваша заявка на вывод {wd['amount']:.2f} ₽ одобрена и обработана.",
        )
        await call.message.edit_text(call.message.text + "\n\n✅ Одобрено")
    else:
        db.set_withdrawal_status(wd_id, "rejected")
        await bot.send_message(
            wd["user_tg_id"],
            f"❌ Ваша заявка на вывод {wd['amount']:.2f} ₽ отклонена. "
            "Свяжитесь с поддержкой для уточнения причины.",
        )
        await call.message.edit_text(call.message.text + "\n\n❌ Отклонено")

    await call.answer("Готово")


async def notify_admins_new_withdrawal(wd_id: int, user, amount: float):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"wd_approve:{wd_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_reject:{wd_id}"),
            ]
        ]
    )
    uname = f"@{user['username']}" if user["username"] else user["first_name"] or user["tg_id"]
    text = (
        "💸 <b>Новая заявка на вывод</b>\n\n"
        f"Пользователь: {uname} (id {user['tg_id']})\n"
        f"Сумма: {amount:.2f} ₽\n"
        f"ID заявки: {wd_id}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# FastAPI backend (serves the Mini App + JSON API)
# ---------------------------------------------------------------------------

bot_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    global bot_task
    bot_task = asyncio.create_task(dp.start_polling(bot))
    yield
    if bot_task:
        bot_task.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


def _auth(x_init_data: str | None) -> tuple[dict, bool]:
    """Валидирует initData, возвращает (tg_user_dict, is_admin)."""
    if not x_init_data:
        raise HTTPException(401, "missing X-Init-Data header")
    try:
        parsed = validate_init_data(x_init_data, BOT_TOKEN)
    except InitDataError as e:
        raise HTTPException(401, f"invalid init data: {e}")
    user = parsed.get("user")
    if not user:
        raise HTTPException(401, "no user in init data")
    return user, user["id"] in ADMIN_IDS


async def require_user(x_init_data: str | None = Header(default=None, alias="X-Init-Data")):
    tg_user, is_admin = _auth(x_init_data)
    row = await run_in_threadpool(
        db.get_or_create_user, tg_user["id"], tg_user.get("username"), tg_user.get("first_name")
    )
    return row, is_admin


async def require_admin(x_init_data: str | None = Header(default=None, alias="X-Init-Data")):
    tg_user, is_admin = _auth(x_init_data)
    if not is_admin:
        raise HTTPException(403, "admin only")
    row = await run_in_threadpool(
        db.get_or_create_user, tg_user["id"], tg_user.get("username"), tg_user.get("first_name")
    )
    return row


# ---- Pydantic models ------------------------------------------------------


class OfferIn(BaseModel):
    category: str  # streamers | youtubers | brands
    title: str
    channel: str
    price: float
    min_views: int = 0          # от скольки просмотров идёт выплата
    image_url: str = ""         # обложка 1920x1080, /assets/uploads/...
    budget_total: float = 0     # общий бюджет оффера, для полоски прогресса
    details: str = ""           # подробные "Детали задания" + правила
    description: str = ""
    hashtag_code: str = ""


class SubmissionIn(BaseModel):
    offer_id: int
    video_url: str
    tiktok_account: str = ""


class WithdrawIn(BaseModel):
    amount: float


class SubmissionStatsIn(BaseModel):
    views: int
    status: str  # accepted | rejected | pending


# ---- Public / user API -----------------------------------------------------


@app.get("/api/me")
async def api_me(auth=None, x_init_data: str | None = Header(default=None, alias="X-Init-Data")):
    user, is_admin = await require_user(x_init_data)
    return {
        "tg_id": user["tg_id"],
        "username": user["username"],
        "balance": user["balance"],
        "is_admin": is_admin,
    }


@app.get("/api/offers")
async def api_offers(category: str | None = None):
    rows = await run_in_threadpool(db.list_offers, category, True)
    return [dict(r) for r in rows]


@app.post("/api/submissions")
async def api_create_submission(
    body: SubmissionIn, x_init_data: str | None = Header(default=None, alias="X-Init-Data")
):
    user, _ = await require_user(x_init_data)
    offer = await run_in_threadpool(db.get_offer, body.offer_id)
    if not offer or not offer["active"]:
        raise HTTPException(404, "offer not found")
    sub_id = await run_in_threadpool(
        db.create_submission, user["id"], body.offer_id, body.video_url, body.tiktok_account
    )
    return {"id": sub_id, "status": "pending"}


@app.get("/api/submissions")
async def api_my_submissions(x_init_data: str | None = Header(default=None, alias="X-Init-Data")):
    user, _ = await require_user(x_init_data)
    rows = await run_in_threadpool(db.list_submissions_for_user, user["id"])
    return [dict(r) for r in rows]


@app.post("/api/withdraw")
async def api_withdraw(
    body: WithdrawIn, x_init_data: str | None = Header(default=None, alias="X-Init-Data")
):
    user, _ = await require_user(x_init_data)
    if body.amount <= 0:
        raise HTTPException(400, "invalid amount")
    if body.amount > user["balance"]:
        raise HTTPException(400, "insufficient balance")

    wd_id = await run_in_threadpool(db.create_withdrawal, user["id"], body.amount)
    await notify_admins_new_withdrawal(wd_id, user, body.amount)
    return {"id": wd_id, "status": "pending", "message": "Заявка отправлена"}


@app.get("/api/withdrawals")
async def api_my_withdrawals(x_init_data: str | None = Header(default=None, alias="X-Init-Data")):
    user, _ = await require_user(x_init_data)
    rows = await run_in_threadpool(db.list_withdrawals, None)
    mine = [dict(r) for r in rows if r["user_id"] == user["id"]]
    return mine


# ---- Admin API --------------------------------------------------------------


@app.get("/api/admin/offers")
async def admin_list_offers(x_init_data: str | None = Header(default=None, alias="X-Init-Data")):
    await require_admin(x_init_data)
    rows = await run_in_threadpool(db.list_offers, None, False)
    return [dict(r) for r in rows]


@app.post("/api/admin/offers")
async def admin_create_offer(
    body: OfferIn, x_init_data: str | None = Header(default=None, alias="X-Init-Data")
):
    await require_admin(x_init_data)
    if body.category not in CATEGORY_LABELS:
        raise HTTPException(400, "invalid category")
    offer_id = await run_in_threadpool(
        db.create_offer,
        body.category,
        body.title,
        body.channel,
        body.price,
        body.description,
        body.hashtag_code,
        body.min_views,
        body.image_url or None,
        body.budget_total,
        body.details,
    )
    return {"id": offer_id}


UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB


@app.post("/api/admin/upload")
async def admin_upload_image(
    file: UploadFile = File(...),
    x_init_data: str | None = Header(default=None, alias="X-Init-Data"),
):
    """Загрузка обложки оффера (рекомендуемый размер 1920x1080)."""
    await require_admin(x_init_data)

    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(400, "разрешены только jpeg/png/webp")

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "файл слишком большой (максимум 8 МБ)")

    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[file.content_type]
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(UPLOAD_DIR, filename)

    def _write():
        with open(dest, "wb") as f:
            f.write(contents)

    await run_in_threadpool(_write)
    return {"url": f"/assets/uploads/{filename}"}


@app.post("/api/admin/offers/{offer_id}/toggle")
async def admin_toggle_offer(
    offer_id: int, x_init_data: str | None = Header(default=None, alias="X-Init-Data")
):
    await require_admin(x_init_data)
    await run_in_threadpool(db.toggle_offer, offer_id)
    return {"ok": True}


@app.delete("/api/admin/offers/{offer_id}")
async def admin_delete_offer(
    offer_id: int, x_init_data: str | None = Header(default=None, alias="X-Init-Data")
):
    await require_admin(x_init_data)
    await run_in_threadpool(db.delete_offer, offer_id)
    return {"ok": True}


@app.get("/api/admin/submissions")
async def admin_list_submissions(x_init_data: str | None = Header(default=None, alias="X-Init-Data")):
    await require_admin(x_init_data)
    rows = await run_in_threadpool(db.list_all_submissions)
    return [dict(r) for r in rows]


@app.post("/api/admin/submissions/{sub_id}/stats")
async def admin_update_submission(
    sub_id: int,
    body: SubmissionStatsIn,
    x_init_data: str | None = Header(default=None, alias="X-Init-Data"),
):
    await require_admin(x_init_data)
    earned = 0.0
    conn_row = await run_in_threadpool(db.list_all_submissions)
    sub = next((r for r in conn_row if r["id"] == sub_id), None)
    if sub:
        offer = await run_in_threadpool(db.get_offer, sub["offer_id"])
        if offer and body.status == "accepted":
            if body.views < (offer["min_views"] or 0):
                # порог для выплаты ещё не набран — видео принято, но пока без начисления
                earned = 0.0
            else:
                earned = (body.views / 1000.0) * offer["price"]
    await run_in_threadpool(db.update_submission_stats, sub_id, body.views, body.status, earned)
    if sub and body.status == "accepted" and earned > 0:
        await run_in_threadpool(db.adjust_balance, sub["user_id"], earned)
        try:
            await bot.send_message(
                sub["user_tg_id"],
                f"🎉 Ваше видео по офферу «{sub['offer_title']}» принято!\n"
                f"Просмотров: {body.views}\nНачислено: {earned:.2f} ₽",
            )
        except Exception:
            pass
    elif sub and body.status == "accepted" and earned == 0:
        try:
            await bot.send_message(
                sub["user_tg_id"],
                f"👀 Ваше видео по офферу «{sub['offer_title']}» принято, "
                f"но пока не набрало минимум просмотров для выплаты ({body.views}). "
                "Как только наберётся нужное количество — сообщите об этом, чтобы обновить статистику.",
            )
        except Exception:
            pass
    elif sub and body.status == "rejected":
        try:
            await bot.send_message(
                sub["user_tg_id"],
                f"😕 Ваше видео по офферу «{sub['offer_title']}» отклонено.",
            )
        except Exception:
            pass
    return {"ok": True, "earned": earned}


@app.get("/api/admin/withdrawals")
async def admin_list_withdrawals(
    status: str | None = None, x_init_data: str | None = Header(default=None, alias="X-Init-Data")
):
    await require_admin(x_init_data)
    rows = await run_in_threadpool(db.list_withdrawals, status)
    return [dict(r) for r in rows]


@app.post("/api/admin/withdrawals/{wd_id}/{action}")
async def admin_decide_withdrawal(
    wd_id: int, action: str, x_init_data: str | None = Header(default=None, alias="X-Init-Data")
):
    await require_admin(x_init_data)
    if action not in ("approve", "reject"):
        raise HTTPException(400, "invalid action")
    wd = await run_in_threadpool(db.get_withdrawal, wd_id)
    if not wd or wd["status"] != "pending":
        raise HTTPException(404, "not found or already processed")
    if action == "approve":
        await run_in_threadpool(db.set_withdrawal_status, wd_id, "approved")
        await run_in_threadpool(db.adjust_balance, wd["uid"], -wd["amount"])
        await bot.send_message(
            wd["user_tg_id"], f"✅ Ваша заявка на вывод {wd['amount']:.2f} ₽ одобрена и обработана."
        )
    else:
        await run_in_threadpool(db.set_withdrawal_status, wd_id, "rejected")
        await bot.send_message(
            wd["user_tg_id"], f"❌ Ваша заявка на вывод {wd['amount']:.2f} ₽ отклонена."
        )
    return {"ok": True}


# ---- Static Mini App files ---------------------------------------------------

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/{page_name}.html")
async def serve_page(page_name: str):
    path = os.path.join(STATIC_DIR, f"{page_name}.html")
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(404)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
