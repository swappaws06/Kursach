"""
=============================================================
  Telegram-бот для автоматического исправления орфографии
=============================================================
      pip install python-telegram-bot==20.7 pyaspeller pymorphy2 razdel

  Запуск:
      python spellcheck_bot_full.py
=============================================================
"""

import os
import re
import sqlite3
import logging
from typing import Dict, Any, List, Tuple

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ── Попытка импорта опциональных библиотек ────────────────────
try:
    import pyaspeller
    ASPELLER_AVAILABLE = True
except ImportError:
    ASPELLER_AVAILABLE = False
    print("ПРЕДУПРЕЖДЕНИЕ: pyaspeller не установлен. pip install pyaspeller")

try:
    import pymorphy2
    morph = pymorphy2.MorphAnalyzer()
    MORPH_AVAILABLE = True
except ImportError:
    MORPH_AVAILABLE = False
    print("ПРЕДУПРЕЖДЕНИЕ: pymorphy2 не установлен. pip install pymorphy2")

# ══════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════



DB_PATH = "spellcheck_bot.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════

def get_connection() -> sqlite3.Connection:
    """Возвращает соединение с включёнными внешними ключами."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_database() -> None:
    """Создаёт все таблицы и индексы при первом запуске."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT NOT NULL,
                last_name   TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active   INTEGER DEFAULT 1 CHECK (is_active IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS checks (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL
                               REFERENCES users(telegram_id) ON DELETE CASCADE,
                original_text  TEXT NOT NULL,
                corrected_text TEXT NOT NULL,
                errors_count   INTEGER DEFAULT 0,
                has_errors     INTEGER DEFAULT 0 CHECK (has_errors IN (0, 1)),
                checked_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS errors (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                check_id   INTEGER NOT NULL
                           REFERENCES checks(id) ON DELETE CASCADE,
                wrong_word TEXT NOT NULL,
                suggestion TEXT NOT NULL,
                position   INTEGER DEFAULT 0,
                error_type TEXT DEFAULT 'spelling'
                           CHECK (error_type IN ('spelling', 'punctuation')),
                lemma      TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id           INTEGER PRIMARY KEY
                                  REFERENCES users(telegram_id) ON DELETE CASCADE,
                check_spelling    INTEGER DEFAULT 1 CHECK (check_spelling IN (0, 1)),
                check_punctuation INTEGER DEFAULT 1 CHECK (check_punctuation IN (0, 1)),
                save_history      INTEGER DEFAULT 1 CHECK (save_history IN (0, 1)),
                language          TEXT DEFAULT 'ru'
            );

            CREATE TABLE IF NOT EXISTS error_stats (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL
                                 REFERENCES users(telegram_id) ON DELETE CASCADE,
                word_lemma       TEXT NOT NULL,
                occurrence_count INTEGER DEFAULT 1,
                last_seen        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, word_lemma)
            );

            CREATE INDEX IF NOT EXISTS idx_checks_user
                ON checks(user_id);
            CREATE INDEX IF NOT EXISTS idx_checks_date
                ON checks(checked_at DESC);
            CREATE INDEX IF NOT EXISTS idx_errors_check
                ON errors(check_id);
            CREATE INDEX IF NOT EXISTS idx_stats_user
                ON error_stats(user_id);
        """)
    logger.info("База данных инициализирована: %s", DB_PATH)


# ── Функции работы с пользователями ──────────────────────────

def db_register_user(
    telegram_id: int,
    username: str,
    first_name: str,
    last_name: str = "",
) -> None:
    """
    Регистрирует нового пользователя или обновляет last_active.
    Использует ON CONFLICT DO UPDATE для идемпотентности.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                last_active = CURRENT_TIMESTAMP,
                username    = excluded.username
            """,
            (telegram_id, username, first_name, last_name),
        )
        conn.execute(
            "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)",
            (telegram_id,),
        )


def db_get_settings(user_id: int) -> Dict[str, Any]:
    """Возвращает настройки пользователя."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else {}


# ── Функции работы с проверками ───────────────────────────────

def db_save_check(
    user_id: int,
    original: str,
    corrected: str,
    errors: list,
) -> int:
    """
    Сохраняет результат проверки в одной транзакции:
      1. Запись в таблицу checks
      2. Записи в таблицу errors (по одной на каждую ошибку)
      3. Обновление таблицы error_stats (INSERT или +1)
    Возвращает ID созданной проверки.
    """
    with get_connection() as conn:
        cursor = conn.cursor()

        # 1. Основная запись проверки
        cursor.execute(
            """
            INSERT INTO checks
                (user_id, original_text, corrected_text, errors_count, has_errors)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, original, corrected, len(errors), 1 if errors else 0),
        )
        check_id = cursor.lastrowid  # ID только что вставленной строки

        # 2 + 3. Каждая ошибка + статистика
        for err in errors:
            cursor.execute(
                """
                INSERT INTO errors
                    (check_id, wrong_word, suggestion, position, error_type, lemma)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    check_id,
                    err["word"],
                    err["suggestion"],
                    err.get("pos", 0),
                    err.get("type", "spelling"),
                    err.get("lemma", ""),
                ),
            )
            if err.get("lemma"):
                cursor.execute(
                    """
                    INSERT INTO error_stats (user_id, word_lemma, occurrence_count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(user_id, word_lemma) DO UPDATE SET
                        occurrence_count = occurrence_count + 1,
                        last_seen        = CURRENT_TIMESTAMP
                    """,
                    (user_id, err["lemma"]),
                )

    return check_id


def db_get_history(user_id: int, limit: int = 5) -> list:
    """Возвращает последние N проверок пользователя."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, original_text, corrected_text, errors_count, checked_at
            FROM checks
            WHERE user_id = ?
            ORDER BY checked_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def db_get_top_errors(user_id: int, limit: int = 5) -> list:
    """Возвращает топ-N наиболее частых ошибок пользователя."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT word_lemma, occurrence_count, last_seen
            FROM error_stats
            WHERE user_id = ?
            ORDER BY occurrence_count DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def db_get_total_stats(user_id: int) -> Dict[str, int]:
    """Возвращает общую статистику пользователя."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)               AS total_checks,
                SUM(errors_count)      AS total_errors,
                SUM(CASE WHEN has_errors = 0 THEN 1 ELSE 0 END) AS clean_checks
            FROM checks
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else {}


# ══════════════════════════════════════════════════════════════
#  ПРОВЕРКА ТЕКСТА
# ══════════════════════════════════════════════════════════════

# Правила пунктуации: (шаблон, замена, описание)
PUNCT_RULES: List[Tuple[str, str, str]] = [
    (r"\s+([,\.!?;:])",   r"\1",    "пробел перед знаком препинания"),
    (r"([,!?;:])([^\s])", r"\1 \2", "отсутствие пробела после знака"),
    (r"\.{2}([^\.])",     r"...\1", "двойная точка вместо многоточия"),
    (r"\s{2,}",           " ",      "лишние пробелы"),
]


def check_spelling(text: str) -> Tuple[str, list]:
    """Орфографическая проверка через Яндекс.Спеллер."""
    errors = []
    if not ASPELLER_AVAILABLE:
        return text, errors
    try:
        checker = pyaspeller.YandexSpeller()
        for item in checker.spell(text):
            if not item["s"]:
                continue
            wrong = item["word"]
            fix   = item["s"][0]
            lemma = ""
            if MORPH_AVAILABLE:
                parsed = morph.parse(wrong)
                if parsed:
                    lemma = parsed[0].normal_form
            errors.append({
                "word":       wrong,
                "suggestion": fix,
                "pos":        item.get("pos", 0),
                "type":       "spelling",
                "lemma":      lemma,
            })
            text = text.replace(wrong, fix, 1)
    except Exception as exc:
        logger.warning("Ошибка Яндекс.Спеллера: %s", exc)
    return text, errors


def check_punctuation(text: str) -> Tuple[str, list]:
    """Пунктуационная проверка по набору правил."""
    errors = []
    for pattern, replacement, description in PUNCT_RULES:
        new_text = re.sub(pattern, replacement, text)
        if new_text != text:
            errors.append({
                "word":       f"(пунктуация: {description})",
                "suggestion": "исправлено",
                "pos":        0,
                "type":       "punctuation",
                "lemma":      "",
            })
            text = new_text
    return text, errors


def check_text(text: str) -> Dict:
    """
    Выполняет полную проверку текста.
    Возвращает: corrected, errors, error_count.
    """
    errors = []
    corrected = text

    corrected, spell_errors = check_spelling(corrected)
    errors.extend(spell_errors)

    corrected, punct_errors = check_punctuation(corrected)
    errors.extend(punct_errors)

    return {
        "corrected":   corrected,
        "errors":      errors,
        "error_count": len(errors),
    }


# ══════════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ TELEGRAM-БОТА
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start — регистрация и приветствие."""
    user = update.effective_user
    db_register_user(user.id, user.username or "", user.first_name, user.last_name or "")
    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n\n"
        "Я проверю орфографию и пунктуацию в твоих текстах на русском языке.\n\n"
        "📌 Просто отправь мне любой текст.\n\n"
        "Команды:\n"
        "/history — история последних проверок\n"
        "/stats   — статистика твоих ошибок\n"
        "/help    — справка"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /help — справка."""
    await update.message.reply_text(
        "🤖 Бот проверки орфографии и пунктуации\n\n"
        "Отправь текст — я:\n"
        "  ✅ Найду орфографические ошибки\n"
        "  ✅ Исправлю пунктуацию\n"
        "  ✅ Покажу список исправлений\n"
        "  ✅ Сохраню результат в историю\n\n"
        "Команды:\n"
        "/start   — начало работы\n"
        "/history — история проверок\n"
        "/stats   — статистика ошибок\n"
        "/help    — эта справка"
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик текстовых сообщений — основная логика."""
    user = update.effective_user
    text = update.message.text

    db_register_user(user.id, user.username or "", user.first_name, user.last_name or "")
    settings = db_get_settings(user.id)
    result   = check_text(text)

    if settings.get("save_history", 1):
        db_save_check(
            user_id=user.id,
            original=text,
            corrected=result["corrected"],
            errors=result["errors"],
        )

    if result["error_count"] == 0:
        response = "✅ Ошибок не найдено! Текст написан корректно."
    else:
        response = (
            f"🔍 Найдено ошибок: {result['error_count']}\n\n"
            f"📝 Исправленный текст:\n{result['corrected']}\n\n"
            "📋 Исправления:\n"
        )
        for i, err in enumerate(result["errors"][:10], start=1):
            if err["type"] == "spelling":
                response += f"{i}. {err['word']} → {err['suggestion']}\n"
            else:
                response += f"{i}. {err['word']}\n"
        if result["error_count"] > 10:
            response += f"... и ещё {result['error_count'] - 10} исправлений."

    await update.message.reply_text(response)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /history — последние 5 проверок из БД."""
    user_id = update.effective_user.id
    records = db_get_history(user_id, limit=5)

    if not records:
        await update.message.reply_text(
            "📭 История пуста. Отправьте любой текст для первой проверки."
        )
        return

    msg = "📚 Последние 5 проверок:\n\n"
    for i, rec in enumerate(records, start=1):
        date    = rec["checked_at"][:10]
        preview = rec["original_text"][:50]
        if len(rec["original_text"]) > 50:
            preview += "..."
        status = f"❌ ошибок: {rec['errors_count']}" if rec["errors_count"] else "✅ без ошибок"
        msg += f"{i}. [{date}] {status}\n   {preview}\n\n"

    await update.message.reply_text(msg)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /stats — статистика из БД."""
    user_id    = update.effective_user.id
    total      = db_get_total_stats(user_id)
    top_errors = db_get_top_errors(user_id, limit=5)

    total_checks = total.get("total_checks", 0)
    total_errors = total.get("total_errors") or 0
    clean_checks = total.get("clean_checks", 0)

    msg  = "📊 Ваша статистика:\n\n"
    msg += f"📝 Всего проверок:   {total_checks}\n"
    msg += f"❌ Всего ошибок:     {total_errors}\n"
    msg += f"✅ Чистых текстов:  {clean_checks}\n"

    if total_checks > 0:
        accuracy = round(clean_checks / total_checks * 100, 1)
        msg += f"🎯 Точность письма: {accuracy}%\n"

    if top_errors:
        msg += "\n🔤 Топ-5 ваших ошибок:\n"
        for i, err in enumerate(top_errors, start=1):
            msg += f"{i}. «{err['word_lemma']}» — {err['occurrence_count']} раз\n"
    else:
        msg += "\nОшибки пока не зафиксированы."

    await update.message.reply_text(msg)


# ══════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ══════════════════════════════════════════════════════════════

def main() -> None:
    if BOT_TOKEN == "ВСТАВЬТЕ_НОВЫЙ_ТОКЕН_ОТ_BOTFATHER":
        raise RuntimeError(
            "Токен не задан! Получите новый токен у @BotFather "
            "и вставьте его в строку BOT_TOKEN в начале файла."
        )

    # Инициализация базы данных
    init_database()

    # Создание и настройка приложения
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_check)
    )

    logger.info("Бот запущен. Ожидание сообщений...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
