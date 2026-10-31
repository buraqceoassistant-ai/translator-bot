"""
Telegram-бот переводчик для рабочих групп (zh / ru / uz).

Логика:
  1. Определяем язык сообщения (по алфавиту, без внешних сервисов).
  2. Простые сообщения → Google Translate (дёшево/бесплатно).
  3. Сложные (термины, цифры с единицами, длинные, смешанные языки)
     или если Google упал → Claude Haiku с глоссарием компании.
  4. Отвечаем reply'ем на оригинал.

Команды:
  /lang zh ru        — языки этой группы (только админы группы)
  /langs             — показать текущие языки
  /pro  (reply)      — перевести это сообщение через Haiku
  /start             — помощь
"""

import asyncio
import base64
import html
import json
import logging
import os
import re
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")            # если пусто → бесплатный deep-translator
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")      # если пусто → Haiku не используется
HAIKU_MODEL = os.getenv("HAIKU_MODEL", "claude-haiku-5-5")
DEFAULT_LANGS = os.getenv("DEFAULT_LANGS", "zh,ru").split(",")
COMPLEX_LEN = int(os.getenv("COMPLEX_LEN", "250"))
# 1 — переводить все фото в группах автоматически; 0 — только по /pro, по подписи "перевод/翻译/tarjima" или в личке
AUTO_PHOTO = os.getenv("AUTO_PHOTO", "0") == "1"
PHOTO_TRIGGERS = ("перевод", "переведи", "翻译", "tarjima", "translate")

APP_DIR = Path(__file__).parent
# На Railway/Docker диск стирается при деплое → данные кладём на Volume.
# Railway сам задаёт RAILWAY_VOLUME_MOUNT_PATH, если к сервису прикреплён Volume.
DATA_DIR = Path(os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or APP_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)
CHATS_FILE = DATA_DIR / "chats.json"
GLOSSARY_FILE = DATA_DIR / "glossary.json"
if not GLOSSARY_FILE.exists() and (APP_DIR / "glossary.json").exists():
    # первый запуск на Volume — копируем глоссарий из репозитория
    GLOSSARY_FILE.write_bytes((APP_DIR / "glossary.json").read_bytes())

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("translator")

FLAGS = {"zh": "🇨🇳", "ru": "🇷🇺", "uz": "🇺🇿", "en": "🇬🇧"}
NAMES = {"zh": "китайский", "ru": "русский", "uz": "узбекский", "en": "английский"}
GOOGLE_CODES = {"zh": "zh-CN", "ru": "ru", "uz": "uz", "en": "en"}

# ---------------------------------------------------------------- хранилище

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


chats: dict[str, list[str]] = load_json(CHATS_FILE, {})
glossary: dict[str, dict[str, str]] = load_json(GLOSSARY_FILE, {})


def chat_langs(chat_id: int) -> list[str]:
    return chats.get(str(chat_id), DEFAULT_LANGS)


# ---------------------------------------------------------------- язык

RE_HAN = re.compile(r"[一-鿿]")
RE_CYR = re.compile(r"[Ѐ-ӿ]")
RE_LAT = re.compile(r"[A-Za-z]")
RE_UZ_CYR = re.compile(r"[ўқғҳЎҚҒҲ]")
RE_UZ_LAT = re.compile(r"(o[‘'ʻ`]|g[‘'ʻ`]|\bsh\b|\bch\b)", re.I)
UZ_WORDS = {"va", "bilan", "uchun", "ham", "bu", "bor", "yoq", "yo'q", "kerak", "qancha",
            "qachon", "narx", "yuk", "bizning", "siz", "men", "rahmat", "salom", "xo'p", "ha"}
EN_WORDS = {"the", "and", "is", "are", "please", "price", "shipping", "cargo", "we", "you", "can"}


def detect_lang(text: str) -> str | None:
    han = len(RE_HAN.findall(text))
    cyr = len(RE_CYR.findall(text))
    lat = len(RE_LAT.findall(text))
    total = han + cyr + lat
    if total == 0:
        return None
    if han / total > 0.3:
        return "zh"
    if cyr >= lat:
        return "uz" if RE_UZ_CYR.search(text) else "ru"
    words = set(re.findall(r"[a-z‘'ʻ`]+", text.lower()))
    uz_score = len(words & UZ_WORDS) + (2 if RE_UZ_LAT.search(text) else 0)
    en_score = len(words & EN_WORDS)
    return "uz" if uz_score >= en_score else "en"


# ---------------------------------------------------------------- "сложное ли сообщение?"

RE_UNITS = re.compile(
    r"\d[\d\s.,]*\s*(кг|kg|т\b|тонн|cbm|м3|м³|куб|方|公斤|吨|件|箱|\$|usd|юан|元|сум|so'm|%|дн|days|天)",
    re.I,
)


def is_complex(text: str, src: str | None) -> bool:
    if len(text) > COMPLEX_LEN:
        return True
    if RE_UNITS.search(text):
        return True
    scripts = sum(bool(r.search(text)) for r in (RE_HAN, RE_CYR, RE_LAT))
    if scripts >= 2:
        return True
    low = text.lower()
    for lang_terms in glossary.values():
        for term in lang_terms.values():
            if term and term.lower() in low:
                return True
    return False


# ---------------------------------------------------------------- Google Translate

async def google_translate(text: str, src: str | None, tgt: str) -> str:
    if GOOGLE_API_KEY:
        params = {"key": GOOGLE_API_KEY, "q": text, "target": GOOGLE_CODES[tgt], "format": "text"}
        if src:
            params["source"] = GOOGLE_CODES[src]
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post("https://translation.googleapis.com/language/translate/v2", data=params)
            r.raise_for_status()
            return r.json()["data"]["translations"][0]["translatedText"]
    # бесплатный вариант без ключа (неофициальный, для теста/малых объёмов)
    from deep_translator import GoogleTranslator
    translator = GoogleTranslator(source=GOOGLE_CODES.get(src, "auto") if src else "auto", target=GOOGLE_CODES[tgt])
    return await asyncio.to_thread(translator.translate, text)


# ---------------------------------------------------------------- Claude Haiku

def glossary_block(src: str, tgt: str) -> str:
    lines = []
    for key, terms in glossary.items():
        if terms.get(src) and terms.get(tgt):
            lines.append(f"- {terms[src]} → {terms[tgt]}")
    return "\n".join(lines)


async def haiku_translate(text: str, src: str | None, tgts: list[str]) -> dict[str, str]:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    src_name = NAMES.get(src, "неизвестный")
    system = (
        "Ты переводчик в рабочем чате логистической компании, которая возит грузы из Китая "
        "в Узбекистан (сборные грузы, контейнеры, авто- и ж/д перевозки, таможня). "
        "Переводи точно и по-деловому, сохраняй числа, единицы, даты, артикулы и имена как есть. "
        "Не добавляй пояснений и комментариев. Отвечай строго JSON-объектом вида "
        '{"ru": "...", "zh": "..."} только с запрошенными языками.'
    )
    gl = "\n\n".join(
        f"Глоссарий {NAMES.get(src, src)} → {NAMES[t]}:\n{glossary_block(src, t)}"
        for t in tgts if src and glossary_block(src, t)
    )
    user = (
        f"Исходный язык: {src_name}. Переведи на: {', '.join(NAMES[t] for t in tgts)} "
        f"(ключи JSON: {', '.join(tgts)}).\n\n"
        + (gl + "\n\n" if gl else "")
        + f"Текст:\n{text}"
    )
    resp = await client.messages.create(
        model=HAIKU_MODEL, max_tokens=2000, system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)
    return {t: data.get(t, "") for t in tgts}


# ---------------------------------------------------------------- фото: OCR + перевод

async def haiku_image(image_bytes: bytes, mime: str, tgts: list[str]) -> dict:
    """Один запрос: распознать текст на фото и перевести. Возвращает {src, text, <lang>: ...}."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    system = (
        "Ты переводчик в рабочем чате логистической компании (грузы из Китая в Узбекистан: "
        "накладные, инвойсы, упаковочные листы, маркировка, скриншоты переписки, этикетки). "
        "Распознай ВЕСЬ текст на изображении, сохраняя строки, числа, единицы, артикулы и таблицы. "
        "Определи его язык и переведи на запрошенные языки. "
        "Отвечай строго JSON без пояснений: "
        '{"src": "zh|ru|uz|en|null", "text": "распознанный текст", "ru": "...", "zh": "..."} '
        "— ключи переводов только для запрошенных языков. Если текста на фото нет, верни {\"src\": null}."
    )
    user_text = (
        f"Языки для перевода: {', '.join(NAMES[t] for t in tgts)} (ключи JSON: {', '.join(tgts)}). "
        "Если исходный язык совпадает с одним из них — этот ключ пропусти."
    )
    gl = "\n".join(
        "- " + " / ".join(terms[l] for l in ("zh", "ru", "uz") if terms.get(l))
        for terms in glossary.values()
    )
    if gl:
        user_text += "\n\nГлоссарий (соответствия терминов zh / ru / uz):\n" + gl
    resp = await client.messages.create(
        model=HAIKU_MODEL, max_tokens=4000, system=system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime,
                                             "data": base64.b64encode(image_bytes).decode()}},
                {"type": "text", "text": user_text},
            ],
        }],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    return json.loads(raw)


async def google_ocr(image_bytes: bytes) -> str:
    """Запасной OCR через Google Cloud Vision (нужен тот же GOOGLE_API_KEY с включённым Vision API)."""
    if not GOOGLE_API_KEY:
        raise RuntimeError("нет GOOGLE_API_KEY для Vision OCR")
    body = {"requests": [{"image": {"content": base64.b64encode(image_bytes).decode()},
                          "features": [{"type": "TEXT_DETECTION"}]}]}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_API_KEY}", json=body)
        r.raise_for_status()
    resp = r.json()["responses"][0]
    return resp.get("fullTextAnnotation", {}).get("text", "")


async def translate_image(image_bytes: bytes, mime: str, langs: list[str]) -> tuple[str | None, str, dict[str, str], str]:
    """Возвращает (src, распознанный_текст, {lang: перевод}, движок)."""
    if ANTHROPIC_API_KEY:
        try:
            data = await haiku_image(image_bytes, mime, langs)
            src = data.get("src") or None
            text = (data.get("text") or "").strip()
            if not src or not text:
                return None, "", {}, "haiku"
            results = {l: data.get(l, "") for l in langs if l != src and data.get(l)}
            return src, text, results, "haiku"
        except Exception as e:  # noqa: BLE001
            log.warning("Haiku vision failed: %s — fallback to Google OCR", e)
    text = (await google_ocr(image_bytes)).strip()
    if not text:
        return None, "", {}, "google"
    src = detect_lang(text)
    tgts = [l for l in langs if l != src]
    results = {t: await google_translate(text, src, t) for t in tgts}
    return src, text, results, "google"


def format_photo_reply(src: str | None, text: str, results: dict[str, str], engine: str) -> str:
    src_flag = FLAGS.get(src, "🌐")
    parts = [f"📷 {src_flag} <blockquote expandable>{html.escape(text)}</blockquote>"]
    for lang, txt in results.items():
        if txt:
            parts.append(f"{src_flag}→{FLAGS[lang]} {html.escape(txt)}")
    tag = " ✨" if engine == "haiku" else ""
    return "\n\n".join(parts) + tag


# ---------------------------------------------------------------- общий перевод

async def translate(text: str, src: str | None, tgts: list[str], force_pro: bool = False) -> tuple[dict[str, str], str]:
    """Возвращает ({lang: перевод}, 'google'|'haiku')."""
    use_haiku = ANTHROPIC_API_KEY and (force_pro or is_complex(text, src))
    if use_haiku:
        try:
            return await haiku_translate(text, src, tgts), "haiku"
        except Exception as e:  # noqa: BLE001
            log.warning("Haiku failed: %s — fallback to Google", e)

    results: dict[str, str] = {}
    try:
        for t in tgts:
            results[t] = await google_translate(text, src, t)
        return results, "google"
    except Exception as e:  # noqa: BLE001
        log.warning("Google failed: %s", e)
        if ANTHROPIC_API_KEY and not use_haiku:
            return await haiku_translate(text, src, tgts), "haiku"
        raise


def format_reply(src: str | None, results: dict[str, str], engine: str) -> str:
    src_flag = FLAGS.get(src, "🌐")
    parts = []
    for lang, txt in results.items():
        if txt:
            parts.append(f"{src_flag}→{FLAGS[lang]} {txt}")
    tag = " ✨" if engine == "haiku" else ""
    return "\n\n".join(parts) + tag


# ---------------------------------------------------------------- бот

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


def is_group(m: Message) -> bool:
    return m.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def pro_keyboard(engine: str, kind: str) -> InlineKeyboardMarkup | None:
    """Кнопка «Точнее» показывается только под переводами Google и только если Haiku настроен."""
    if engine != "google" or not ANTHROPIC_API_KEY:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Точнее (ИИ)", callback_data=f"pro:{kind}")
    ]])


async def is_admin(m: Message) -> bool:
    if not is_group(m):
        return True
    member = await bot.get_chat_member(m.chat.id, m.from_user.id)
    return member.status in ("administrator", "creator")


@dp.message(Command("start", "help"))
async def cmd_start(m: Message):
    langs = chat_langs(m.chat.id)
    await m.answer(
        "Я переводчик для рабочих чатов.\n\n"
        f"Языки здесь: {' ↔ '.join(FLAGS[l] + ' ' + NAMES[l] for l in langs)}\n\n"
        "Команды:\n"
        "/lang zh ru uz — задать языки (админы)\n"
        "/langs — текущие языки\n"
        "/pro — ответом на сообщение или фото: точный перевод через ИИ\n"
        "Или просто нажмите кнопку «🔁 Точнее» под переводом.\n\n"
        "📷 Фото: в личке переведу любое фото с текстом. В группе — ответьте на фото /pro "
        "или отправьте фото с подписью «перевод» / «翻译» / «tarjima».\n\n"
        "В личке просто пришлите текст или фото — переведу на остальные языки.",
    )


@dp.message(Command("langs"))
async def cmd_langs(m: Message):
    langs = chat_langs(m.chat.id)
    await m.answer("Языки чата: " + ", ".join(f"{FLAGS[l]} {NAMES[l]}" for l in langs))


@dp.message(Command("lang"))
async def cmd_lang(m: Message, command: CommandObject):
    if not await is_admin(m):
        return await m.reply("Только админы группы могут менять языки.")
    codes = [c.strip().lower() for c in (command.args or "").split()]
    bad = [c for c in codes if c not in FLAGS]
    if len(codes) < 2 or bad:
        return await m.reply(f"Пример: /lang zh ru  или  /lang zh ru uz\nДоступно: {', '.join(FLAGS)}")
    chats[str(m.chat.id)] = codes
    save_json(CHATS_FILE, chats)
    await m.reply("Готово: " + " ↔ ".join(f"{FLAGS[c]} {NAMES[c]}" for c in codes))


async def handle_text(m: Message, text: str, force_pro: bool = False):
    text = text.strip()
    if not text or text.startswith("/"):
        return
    src = detect_lang(text)
    langs = chat_langs(m.chat.id)
    tgts = [l for l in langs if l != src]
    if not tgts:
        return
    if src not in langs and not force_pro:
        # язык не из настроенных для этой группы — в группе молчим, в личке переводим на все
        if is_group(m):
            return
    try:
        results, engine = await translate(text, src, tgts, force_pro=force_pro)
    except Exception as e:  # noqa: BLE001
        log.exception("translate failed")
        return await m.reply(f"Не удалось перевести: {e}")
    await m.reply(format_reply(src, results, engine), reply_markup=pro_keyboard(engine, "text"))


def image_file(m: Message):
    """(file_id, mime) если в сообщении есть картинка — сжатое фото или картинка-файл."""
    if m.photo:
        return m.photo[-1].file_id, "image/jpeg"
    if m.document and (m.document.mime_type or "").startswith("image/"):
        mime = m.document.mime_type
        if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            mime = "image/jpeg"
        return m.document.file_id, mime
    return None


async def handle_image(m: Message, quiet_if_empty: bool):
    info = image_file(m)
    if not info:
        return
    file_id, mime = info
    if not ANTHROPIC_API_KEY and not GOOGLE_API_KEY:
        return await m.reply("Перевод фото не настроен: нужен ANTHROPIC_API_KEY или GOOGLE_API_KEY с Vision API.")
    tg_file = await bot.get_file(file_id)
    buf = await bot.download_file(tg_file.file_path)
    image_bytes = buf.read()
    langs = chat_langs(m.chat.id)
    try:
        src, text, results, engine = await translate_image(image_bytes, mime, langs)
    except Exception as e:  # noqa: BLE001
        log.exception("image translate failed")
        return await m.reply(f"Не удалось перевести фото: {e}")
    if not text:
        if not quiet_if_empty:
            await m.reply("Текста на фото не нашёл.")
        return
    await m.reply(format_photo_reply(src, text, results, engine), parse_mode=ParseMode.HTML,
                  reply_markup=pro_keyboard(engine, "photo"))


@dp.callback_query(F.data.startswith("pro:"))
async def on_pro_button(cb: CallbackQuery):
    """Кнопка «Точнее»: переводим исходное сообщение через Haiku и редактируем наш ответ."""
    kind = cb.data.split(":", 1)[1]
    original = cb.message.reply_to_message if cb.message else None
    if not original:
        return await cb.answer("Исходное сообщение не найдено.", show_alert=True)
    await cb.answer("Перевожу через ИИ…")
    try:
        if kind == "photo" and image_file(original):
            file_id, mime = image_file(original)
            tg_file = await bot.get_file(file_id)
            image_bytes = (await bot.download_file(tg_file.file_path)).read()
            data = await haiku_image(image_bytes, mime, chat_langs(original.chat.id))
            src = data.get("src") or None
            text = (data.get("text") or "").strip()
            results = {l: data.get(l, "") for l in chat_langs(original.chat.id) if l != src and data.get(l)}
            await cb.message.edit_text(format_photo_reply(src, text, results, "haiku"),
                                       parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            text = (original.text or original.caption or "").strip()
            src = detect_lang(text)
            tgts = [l for l in chat_langs(original.chat.id) if l != src]
            results = await haiku_translate(text, src, tgts)
            await cb.message.edit_text(format_reply(src, results, "haiku"), reply_markup=None)
    except Exception as e:  # noqa: BLE001
        log.exception("pro button failed")
        await cb.answer(f"Не получилось: {e}", show_alert=True)


@dp.message(Command("pro"))
async def cmd_pro(m: Message):
    target = m.reply_to_message
    if not target:
        return await m.reply("Ответьте командой /pro на сообщение или фото, которое нужно перевести точно.")
    if not ANTHROPIC_API_KEY:
        return await m.reply("ИИ-перевод не настроен (нет ANTHROPIC_API_KEY).")
    if image_file(target):
        return await handle_image(target, quiet_if_empty=False)
    if target.text or target.caption:
        return await handle_text(target, target.text or target.caption, force_pro=True)
    await m.reply("В этом сообщении нет текста или фото.")


@dp.message(F.text)
async def on_text(m: Message):
    await handle_text(m, m.text)


@dp.message(F.photo | F.document)
async def on_image(m: Message):
    if not image_file(m):
        return
    caption = (m.caption or "").strip()
    cap_low = caption.lower()
    triggered = any(t in cap_low for t in PHOTO_TRIGGERS)
    # подпись переводим как обычный текст, если это не просто команда "переведи"
    if caption and not triggered:
        await handle_text(m, caption)
    # само фото: в личке всегда; в группе — если AUTO_PHOTO, или подпись просит перевод
    if not is_group(m) or AUTO_PHOTO or triggered:
        await handle_image(m, quiet_if_empty=is_group(m) and not triggered)


@dp.message(F.caption)
async def on_caption(m: Message):
    await handle_text(m, m.caption)


async def main():
    log.info("Bot started. Data dir: %s, default langs: %s, Google key: %s, Haiku: %s",
             DATA_DIR, DEFAULT_LANGS, bool(GOOGLE_API_KEY), bool(ANTHROPIC_API_KEY))
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
