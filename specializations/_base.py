"""
specializations/_base.py — Фабрика обработчиков для VK Teams.
Каждая специализация вызывает register_handlers() с своими константами.
Все хэндлеры регистрируются в глобальном диспетчере.
"""
import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vk_bot.bot import VKBot
    from vk_bot.types import VKMessage, VKCallbackQuery

from library.models import CurrentTestState
from library.states import TestStates
from library.state_manager import state_manager
from library.question_loader import load_questions_for_specialization
from library.enum import Difficulty
from library.keyboards import (
    get_difficulty_keyboard, get_finish_keyboard, get_main_keyboard
)
from library.core import (
    show_question, handle_answer_toggle,
    handle_next_question, finish_test
)
from library.timers import create_timer
from library.stats import stats_manager
from config.settings import settings

logger = logging.getLogger(__name__)


HELP_TEXT = (
    "❓ <b>Помощь по боту</b>\n\n"
    "<b>Как пройти тест:</b>\n"
    "1️⃣ Выберите специализацию\n"
    "2️⃣ Введите данные (ФИО, должность, подразделение)\n"
    "3️⃣ Выберите уровень сложности\n"
    "4️⃣ Отвечайте на вопросы (1️⃣2️⃣3️⃣...)\n"
    "5️⃣ Нажмите ➡️ Далее\n"
    "6️⃣ Получите результат и сертификат\n\n"
    "<b>Уровни сложности:</b>\n"
    "🥉 Резерв: 20 вопросов, 35 мин\n"
    "🥈 Базовый: 30 вопросов, 25 мин\n"
    "🥇 Стандартный: 40 вопросов, 20 мин\n"
    "💎 Продвинутый: 50 вопросов, 20 мин\n\n"
    "Удачи! 🍀"
)




def make_handlers(spec_name: str, spec_label: str, spec_emoji: str):
    """
    Возвращает словарь хэндлеров для данной специализации.
    
    Ключи словаря соответствуют callback_data,
    значения — async функции (bot, query/message, user_id).
    """

    # ------------------------------------------------------------------ #
    # Шаг 1: Выбор специализации
    # ------------------------------------------------------------------ #
    async def on_select_spec(bot: "VKBot", query: "VKCallbackQuery", user_id: str):
        chat_id = query.message.chat.chatId
        await bot.answer_callback(query.queryId)
        # Редактируем меню (не удаляем — VK Teams оставляет "Сообщение удалено")
        try:
            await bot.edit_text(
                chat_id, query.message.msgId,
                f"{spec_emoji} <b>{spec_label}</b>\n\nВведите ваше ФИО:"
            )
        except Exception:
            await bot.send_text(
                chat_id,
                f"{spec_emoji} <b>{spec_label}</b>\n\nВведите ваше ФИО:"
            )
        await state_manager.set_state(user_id, TestStates.WAITING_FULL_NAME)
        await state_manager.update_data(user_id, specialization=spec_name)

    # ------------------------------------------------------------------ #
    # Шаги 2–4: Сбор данных пользователя (текстовые сообщения)
    # ------------------------------------------------------------------ #
    async def on_full_name(bot: "VKBot", message: "VKMessage", user_id: str):
        await state_manager.update_data(user_id, full_name=message.text.strip())
        await bot.send_text(message.chat.chatId, "Введите вашу должность:")
        await state_manager.set_state(user_id, TestStates.WAITING_POSITION)

    async def on_position(bot: "VKBot", message: "VKMessage", user_id: str):
        await state_manager.update_data(user_id, position=message.text.strip())
        await bot.send_text(message.chat.chatId, "Введите ваше подразделение:")
        await state_manager.set_state(user_id, TestStates.WAITING_DEPARTMENT)

    async def on_department(bot: "VKBot", message: "VKMessage", user_id: str):
        await state_manager.update_data(user_id, department=message.text.strip())
        await bot.send_text(
            message.chat.chatId,
            "Выберите уровень сложности:",
            get_difficulty_keyboard()
        )
        await state_manager.set_state(user_id, TestStates.WAITING_DIFFICULTY)

    # ------------------------------------------------------------------ #
    # Шаг 5: Выбор сложности → старт теста
    # ------------------------------------------------------------------ #
    async def on_difficulty(bot: "VKBot", query: "VKCallbackQuery", user_id: str):
        await bot.answer_callback(query.queryId)
        diff_value = query.callbackData.split("_", 1)[1]
        
        try:
            difficulty = Difficulty(diff_value)
        except ValueError:
            await bot.answer_callback(query.queryId, "❌ Неверный уровень сложности", True)
            return
        
        user_data = await state_manager.get_data(user_id)
        specialization = user_data.get("specialization", spec_name)
        
        questions = load_questions_for_specialization(specialization, difficulty, user_id)
        if not questions:
            chat_id = query.message.chat.chatId
            try:
                await bot.edit_text(chat_id, query.message.msgId,
                    "❌ Не удалось загрузить вопросы. Попробуйте позже.")
            except Exception:
                await bot.send_text(chat_id, "❌ Не удалось загрузить вопросы. Попробуйте позже.")
            await state_manager.clear(user_id)
            return
        
        test_state = CurrentTestState(
            questions=questions,
            specialization=specialization,
            difficulty=difficulty,
            full_name=user_data.get("full_name", ""),
            position=user_data.get("position", ""),
            department=user_data.get("department", "")
        )
        
        chat_id = query.message.chat.chatId
        
        async def on_timeout():
            try:
                # Читаем актуальный test_state из state_manager (он обновлялся по ходу теста)
                current_data = await state_manager.get_data(user_id)
                current_test_state = current_data.get("test_state", test_state)
                
                # Сразу блокируем дальнейшие ответы
                await state_manager.set_state(user_id, TestStates.SHOWING_RESULTS)
                
                await finish_test(bot, query, user_id, current_test_state, timed_out=True)
            except Exception as e:
                logger.error(f"❌ Ошибка таймаута: {e}", exc_info=True)
        
        timer = create_timer(difficulty, on_timeout)
        await timer.start()
        test_state.timer_task = timer
        
        await stats_manager.update_user_activity(user_id)
        
        await state_manager.set_state(user_id, TestStates.ANSWERING_QUESTION)
        
        # Устанавливаем ID сообщения выбора сложности — первый вопрос отредактирует его
        # вместо отправки нового сообщения. Так кнопки сложности исчезают без следа.
        test_state.last_message_id = str(query.message.msgId)
        
        await state_manager.update_data(user_id, test_state=test_state)
        
        await show_question(bot, chat_id, test_state, question_index=0)
        await state_manager.update_data(user_id, test_state=test_state)
        
        logger.info(f"▶️ {user_id} начал {specialization} ({difficulty.value})")

    # ------------------------------------------------------------------ #
    # Прохождение теста
    # ------------------------------------------------------------------ #
    async def on_answer(bot: "VKBot", query: "VKCallbackQuery", user_id: str):
        await handle_answer_toggle(bot, query, user_id)

    async def on_next(bot: "VKBot", query: "VKCallbackQuery", user_id: str):
        await handle_next_question(bot, query, user_id)

    # ------------------------------------------------------------------ #
    # Результаты: показ правильных ответов
    # ------------------------------------------------------------------ #
    async def on_show_answers(bot: "VKBot", query: "VKCallbackQuery", user_id: str):
        data = await state_manager.get_data(user_id)
        test_state: CurrentTestState = data.get("test_state")
        if not test_state:
            await bot.answer_callback(query.queryId, "❌ Данные теста не найдены", True)
            return
        
        answers_text = "📋 <b>Правильные ответы:</b>\n\n"
        for i, question in enumerate(test_state.questions, 1):
            user_answer = test_state.answers_history.get(i - 1, set())
            correct = question.correct_answers
            emoji = "✅" if user_answer == correct else "❌"
            nums = ", ".join(str(n) for n in sorted(correct))
            answers_text += f"{emoji} <b>Вопрос {i}:</b> {nums}\n"
        answers_text += f"\n⏱ <i>Сообщение удалится через {settings.answers_show_time} сек</i>"
        
        chat_id = query.message.chat.chatId
        await bot.answer_callback(query.queryId)
        resp = await bot.send_text(chat_id, answers_text)
        
        if resp and resp.get("ok"):
            msg_id = str(resp.get("msgId", ""))
            async def clear_answers():
                await asyncio.sleep(settings.answers_show_time)
                try:
                    await bot.edit_text(chat_id, msg_id,
                        "📋 Правильные ответы скрыты. Для повтора нажмите кнопку снова.")
                except Exception:
                    pass
            asyncio.create_task(clear_answers())

    # ------------------------------------------------------------------ #
    # Текстовый сертификат (с автоскрытием)
    # ------------------------------------------------------------------ #
    async def on_generate_cert(bot: "VKBot", query: "VKCallbackQuery", user_id: str):
        data = await state_manager.get_data(user_id)
        test_state: CurrentTestState = data.get("test_state")
        if not test_state:
            await bot.answer_callback(query.queryId, "❌ Данные теста не найдены", True)
            return

        await bot.answer_callback(query.queryId, "📄 Формирую сертификат...")
        chat_id = query.message.chat.chatId

        from datetime import datetime
        grade_emoji = {
            "отлично": "🏆", "хорошо": "👍",
            "удовлетворительно": "👌", "неудовлетворительно": "❌"
        }
        g_emoji = grade_emoji.get(test_state.grade, "📊")

        cert_text = (
            f"📜 <b>СЕРТИФИКАТ</b>\n"
            f"<i>о прохождении профессионального тестирования</i>\n\n"
            f"👤 <b>ФИО:</b> {test_state.full_name}\n"
            f"💼 <b>Должность:</b> {test_state.position}\n"
            f"🏢 <b>Подразделение:</b> {test_state.department}\n\n"
            f"📚 <b>Специализация:</b> {test_state.specialization.upper()}\n"
            f"📊 <b>Уровень:</b> {test_state.difficulty.value.capitalize()}\n\n"
            f"{g_emoji} <b>Оценка:</b> {test_state.grade.upper()}\n"
            f"✅ <b>Правильных ответов:</b> {test_state.correct_count} из {test_state.total_questions}\n"
            f"💯 <b>Результат:</b> {test_state.percentage:.1f}%\n"
            f"⏱ <b>Время:</b> {test_state.elapsed_time}\n\n"
            f"📅 <b>Дата:</b> {datetime.now().strftime('%d.%m.%Y')}\n"
            f"🆔 <b>ID:</b> {user_id}\n\n"
            f"<i>ФССП РОССИИ — Система тестирования профессиональной подготовки</i>\n\n"
            f"⏱ <i>Сертификат скроется через {settings.answers_show_time} сек</i>"
        )

        resp = await bot.send_text(chat_id, cert_text)

        # Автоскрытие через 60 секунд — как статистика и правильные ответы
        if resp and resp.get("ok"):
            msg_id = str(resp.get("msgId", ""))
            async def hide_cert():
                await asyncio.sleep(settings.answers_show_time)
                try:
                    await bot.edit_text(chat_id, msg_id,
                        "📜 Сертификат скрыт. Для повтора нажмите кнопку снова.")
                except Exception:
                    pass
            asyncio.create_task(hide_cert())

    # ------------------------------------------------------------------ #
    # Повторить тест
    # ------------------------------------------------------------------ #
    async def on_repeat(bot: "VKBot", query: "VKCallbackQuery", user_id: str):
        await state_manager.clear(user_id)
        chat_id = query.message.chat.chatId
        await bot.answer_callback(query.queryId)
        try:
            await bot.edit_text(
                chat_id, query.message.msgId,
                f"{spec_emoji} <b>{spec_label}</b>\n\nВведите ваше ФИО:"
            )
        except Exception:
            await bot.send_text(
                chat_id,
                f"{spec_emoji} <b>{spec_label}</b>\n\nВведите ваше ФИО:"
            )
        await state_manager.set_state(user_id, TestStates.WAITING_FULL_NAME)
        await state_manager.update_data(user_id, specialization=spec_name)

    # ------------------------------------------------------------------ #
    # Статистика
    # ------------------------------------------------------------------ #
    async def on_stats(bot: "VKBot", query: "VKCallbackQuery", user_id: str):
        try:
            stats = await stats_manager.get_user_stats(user_id)
            if stats.get("total_tests", 0) == 0:
                text = (
                    "📊 <b>Ваша статистика</b>\n\n"
                    "У вас пока нет пройденных тестов.\n"
                    "Начните тестирование прямо сейчас!"
                )
            else:
                text = (
                    f"📊 <b>Ваша статистика</b>\n\n"
                    f"📝 Всего тестов: {stats['total_tests']}\n"
                    f"📈 Средний балл: {stats['avg_percentage']}%\n"
                    f"🏆 Лучший результат: {stats['best_result']}%\n"
                    f"📉 Худший результат: {stats['worst_result']}%"
                )
                if stats.get("recent_tests"):
                    text += "\n\n<b>Последние тесты:</b>\n"
                    for r in stats["recent_tests"]:
                        text += (
                            f"• {r['specialization']} ({r['difficulty']}): "
                            f"{r['grade']} — {r['percentage']:.1f}%\n"
                        )
            await bot.answer_callback(query.queryId)
            chat_id = query.message.chat.chatId
            resp = await bot.send_text(chat_id, text)
            
            # Автоскрытие через 60 секунд — как у правильных ответов
            if resp and resp.get("ok"):
                msg_id = str(resp.get("msgId", ""))
                async def hide_stats():
                    await asyncio.sleep(settings.answers_show_time)
                    try:
                        await bot.edit_text(chat_id, msg_id,
                            "📋 Статистика скрыта. Для повтора нажмите кнопку снова.")
                    except Exception:
                        pass
                asyncio.create_task(hide_stats())
        except Exception as e:
            logger.error(f"❌ Ошибка статистики: {e}", exc_info=True)
            await bot.answer_callback(query.queryId, "❌ Ошибка загрузки", True)

    # ------------------------------------------------------------------ #
    # Главное меню
    # ------------------------------------------------------------------ #
    async def on_main_menu(bot: "VKBot", query: "VKCallbackQuery", user_id: str):
        from main import MAIN_MENU_TEXT as _MENU_TEXT
        await state_manager.clear(user_id)
        chat_id = query.message.chat.chatId
        await bot.answer_callback(query.queryId)
        try:
            await bot.edit_text(
                chat_id, query.message.msgId,
                _MENU_TEXT, get_main_keyboard()
            )
        except Exception:
            await bot.send_text(chat_id, _MENU_TEXT, get_main_keyboard())

    # ------------------------------------------------------------------ #
    # Помощь
    # ------------------------------------------------------------------ #
    async def on_help(bot: "VKBot", query: "VKCallbackQuery", user_id: str):
        await bot.answer_callback(query.queryId)
        chat_id = query.message.chat.chatId
        resp = await bot.send_text(chat_id, HELP_TEXT)

        # Автоскрытие через 60 секунд
        if resp and resp.get("ok"):
            msg_id = str(resp.get("msgId", ""))
            async def hide_help():
                await asyncio.sleep(settings.answers_show_time)
                try:
                    await bot.edit_text(chat_id, msg_id,
                        "❓ Помощь скрыта. Для повтора нажмите кнопку снова.")
                except Exception:
                    pass
            asyncio.create_task(hide_help())

    return {
        # Callback handlers (keyed by callbackData prefix/exact)
        f"spec_{spec_name}": on_select_spec,
        "diff_": on_difficulty,         # prefix match
        "ans_":  on_answer,             # prefix match
        "next":  on_next,
        "show_answers":  on_show_answers,
        "generate_cert": on_generate_cert,
        "repeat_test":   on_repeat,
        "my_stats":      on_stats,
        "main_menu":     on_main_menu,
        "help":          on_help,
        # Message handlers (keyed by state)
        f"msg:{TestStates.WAITING_FULL_NAME}":  on_full_name,
        f"msg:{TestStates.WAITING_POSITION}":   on_position,
        f"msg:{TestStates.WAITING_DEPARTMENT}": on_department,
    }
