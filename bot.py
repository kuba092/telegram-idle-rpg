import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

players: dict[int, dict[str, int]] = {}


def get_player(user_id: int) -> dict[str, int]:
    if user_id not in players:
        players[user_id] = {
            "level": 1,
            "gold": 0,
            "energy": 10,
        }
    return players[user_id]


def main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("⚔️ В бой", callback_data="fight")],
        [
            InlineKeyboardButton("👤 Герой", callback_data="profile"),
            InlineKeyboardButton("💰 Забрать золото", callback_data="gold"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    player = get_player(update.effective_user.id)

    await update.message.reply_text(
        "⚔️ Добро пожаловать в Idle RPG!\n\n"
        f"Уровень: {player['level']}\n"
        f"Золото: {player['gold']}\n"
        f"Энергия: {player['energy']}/10",
        reply_markup=main_menu(),
    )


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    player = get_player(query.from_user.id)

    if query.data == "fight":
        if player["energy"] <= 0:
            text = "😴 Энергия закончилась. Нужно отдохнуть."
        else:
            player["energy"] -= 1
            player["gold"] += 5
            text = (
                "⚔️ Ты победил слизня!\n"
                "Получено: 5 золота\n\n"
                f"Золото: {player['gold']}\n"
                f"Энергия: {player['energy']}/10"
            )

    elif query.data == "profile":
        text = (
            "👤 Твой герой\n\n"
            f"Уровень: {player['level']}\n"
            f"Золото: {player['gold']}\n"
            f"Энергия: {player['energy']}/10"
        )

    elif query.data == "gold":
        player["gold"] += 10
        text = (
            "💰 Ты забрал награду: 10 золота!\n\n"
            f"Всего золота: {player['gold']}"
        )

    else:
        text = "Неизвестная команда."

    await query.edit_message_text(text, reply_markup=main_menu())


def main() -> None:
    token = os.getenv("BOT_TOKEN")

    if not token:
        raise RuntimeError("Не найден BOT_TOKEN")

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))

    application.run_polling()


if __name__ == "__main__":
    main()
