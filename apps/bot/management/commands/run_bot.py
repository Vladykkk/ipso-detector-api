"""
Management command to run the Telegram bot in long-polling mode.
Used for local development — no public URL or ngrok needed.

Usage:
    python manage.py run_bot
"""
import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from asgiref.sync import sync_to_async

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler as TgCommandHandler,
    MessageHandler as TgMessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from apps.analyzer.pipeline import analyze_text
from apps.core.models import AnalysisResult, TelegramAnalysis, Feedback
from apps.bot.formatters import ResultFormatter

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run Telegram bot in long-polling mode (for development)'

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token or token == 'placeholder-token':
            raise CommandError(
                'TELEGRAM_BOT_TOKEN is not configured. '
                'Set it in .env file (get token from @BotFather).'
            )

        self.stdout.write(self.style.SUCCESS('Starting IPSO Detector bot (polling mode)...'))

        app = ApplicationBuilder().token(token).build()

        # Commands
        app.add_handler(TgCommandHandler('start', self._cmd_start))
        app.add_handler(TgCommandHandler('help', self._cmd_help))
        app.add_handler(TgCommandHandler('stats', self._cmd_stats))

        # Text messages → analysis
        app.add_handler(TgMessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            self._handle_message,
        ))

        # Inline keyboard callbacks (feedback buttons)
        app.add_handler(CallbackQueryHandler(self._handle_callback))

        self.stdout.write(f'Bot is running. Press Ctrl+C to stop.')
        app.run_polling(drop_pending_updates=True)

    # ── Command handlers ──────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_html(
            "<b>IPSO Detector Bot</b>\n\n"
            "Я аналізую тексти на ознаки російських ІПСО "
            "(інформаційно-психологічних операцій).\n\n"
            "<b>Як користуватися:</b>\n"
            "1. Надішліть або перешліть текст повідомлення\n"
            "2. Отримайте результат аналізу за декілька секунд\n\n"
            "<b>Команди:</b>\n"
            "/start — Почати роботу\n"
            "/help — Довідка\n"
            "/stats — Статистика бота"
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_html(
            "<b>Довідка IPSO Detector</b>\n\n"
            "Система аналізує текст за трьома модулями:\n"
            "1. <b>Наративи</b> — AI-класифікація за 7 типами ІПСО\n"
            "2. <b>Риторика</b> — виявлення маніпулятивних технік\n"
            "3. <b>Подібність</b> — порівняння з базою відомих ІПСО\n\n"
            "Результат: підсумковий бал від 0% до 100%\n"
            "• <b>0–35%</b> — Безпечно ✅\n"
            "• <b>35–70%</b> — Підозрілий ⚠️\n"
            "• <b>70–100%</b> — ІПСО виявлено 🚨\n\n"
            "Мінімальна довжина тексту: 30 символів."
        )

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        stats = await sync_to_async(self._get_stats)()
        await update.message.reply_html(
            "<b>📊 Статистика бота</b>\n\n"
            f"Всього аналізів: <b>{stats['total']}</b>\n"
            f"  🚨 ІПСО: {stats['ipso']}\n"
            f"  ⚠️ Підозрілий: {stats['suspicious']}\n"
            f"  ✅ Безпечно: {stats['safe']}\n\n"
            f"Відгуків отримано: {stats['feedbacks']}"
        )

    @staticmethod
    def _get_stats() -> dict:
        return {
            'total': AnalysisResult.objects.filter(source='telegram').count(),
            'ipso': AnalysisResult.objects.filter(source='telegram', verdict='ipso').count(),
            'suspicious': AnalysisResult.objects.filter(source='telegram', verdict='suspicious').count(),
            'safe': AnalysisResult.objects.filter(source='telegram', verdict='safe').count(),
            'feedbacks': Feedback.objects.count(),
        }

    # ── Message handler (analysis) ────────────────────────────────

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or update.message.caption or '').strip()
        chat_id = update.effective_chat.id
        username = update.effective_user.username or ''
        message_id = update.message.message_id

        if len(text) < 30:
            await update.message.reply_text(
                "Текст занадто короткий. Мінімум 30 символів для аналізу."
            )
            return

        # Send "analyzing" indicator
        status_msg = await update.message.reply_text("🔍 Аналізую текст...")

        try:
            # Run sync analysis pipeline in a thread
            result = await sync_to_async(self._run_analysis)(
                text, chat_id, message_id, username,
            )

            # Format and send result
            response_text = ResultFormatter.format(result)

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("👍 Правильно", callback_data=f"feedback_correct_{result.id}"),
                    InlineKeyboardButton("👎 Помилка", callback_data=f"feedback_wrong_{result.id}"),
                ]
            ])

            # Delete "analyzing" message and send result
            await status_msg.delete()
            await update.message.reply_html(response_text, reply_markup=keyboard)

        except Exception as e:
            logger.error("Analysis failed in bot: %s", e, exc_info=True)
            await status_msg.edit_text("❌ Виникла помилка при аналізі. Спробуйте пізніше.")

    @staticmethod
    def _run_analysis(text: str, chat_id: int, message_id: int, username: str):
        """Run the full analysis pipeline (synchronous — called via sync_to_async)."""
        result = analyze_text(text, source='telegram')
        TelegramAnalysis.objects.create(
            result=result,
            chat_id=chat_id,
            message_id=message_id,
            username=username,
        )
        return result

    # ── Callback handler (feedback) ───────────────────────────────

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data

        if not data.startswith('feedback_'):
            await query.answer()
            return

        parts = data.split('_')
        if len(parts) != 3:
            await query.answer("Невірний формат")
            return

        feedback_type = parts[1]  # correct / wrong
        try:
            result_id = int(parts[2])
        except ValueError:
            await query.answer("Невірний ID")
            return

        chat_id = query.message.chat.id
        response = await sync_to_async(self._save_feedback)(
            result_id, feedback_type, chat_id,
        )

        await query.answer(response)

        # Remove inline buttons after feedback
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    @staticmethod
    def _save_feedback(result_id: int, feedback_type: str, chat_id: int) -> str:
        """Save feedback to DB (synchronous — called via sync_to_async)."""
        existing = Feedback.objects.filter(
            result_id=result_id, chat_id=chat_id,
        ).exists()
        if existing:
            return "Ви вже залишали відгук для цього аналізу."

        Feedback.objects.create(
            result_id=result_id,
            feedback_type=feedback_type,
            chat_id=chat_id,
        )
        emoji = "👍" if feedback_type == 'correct' else "👎"
        return f"Дякуємо за відгук! {emoji}"
