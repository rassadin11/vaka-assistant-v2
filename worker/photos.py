"""Photo-envelope OCR that feeds recognized text into the normal agent flow."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import logging
import os
from io import BytesIO

import pytesseract
from PIL import Image, UnidentifiedImageError

from core.context import TaskContext
from core.envelope import UpdateEnvelope
from core.limits import message_limit_reached, monthly_pdf_pages_limit
from core.profile import ProfileLimits, profile_limits
from core.spend import BudgetState, budget_state, daily_budget_rub, get_spent_rub
from worker.documents import (
    MONTHLY_KEY_TTL_SECONDS,
    MONTHLY_LIMIT_TEXT,
    DownloadFile,
    QueueRedis,
    _monthly_pages_key,
    downscaled,
)
from worker.processor import ContextualProcessor
from worker.reply import WorkerReply
from worker.voice import MESSAGE_LIMIT_TEXT, SOFT_REFUSE_TEXT

PHOTO_TOO_LARGE_TEMPLATE = "Фото больше {megabytes} МБ не поддерживаются."
PHOTO_DOWNLOAD_UNAVAILABLE_TEXT = "Скачивание фото временно недоступно, пришлите ещё раз позже."
PHOTO_OCR_UNAVAILABLE_TEXT = "Распознавание фото временно недоступно."
INVALID_PHOTO_TEXT = "Не удалось обработать фото."
EMPTY_PHOTO_TEXT = "Не нашёл текста на фото. Пришлите изображение с текстом или добавьте подпись."

LOGGER = logging.getLogger(__name__)


class PhotoOcrProcessor:
    """Recognize one photo and delegate its text to the active contextual processor."""

    def __init__(
        self,
        queue_redis: QueueRedis,
        download_file: DownloadFile | None,
        inner: ContextualProcessor,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._queue_redis = queue_redis
        self._download_file = download_file
        self._inner = inner
        self._logger = logger if logger is not None else LOGGER

    async def process(
        self, envelope: UpdateEnvelope, context: TaskContext
    ) -> str | WorkerReply | None:
        """Apply limits, OCR the photo, and pass caption plus text to the agent."""

        try:
            if await message_limit_reached(
                self._queue_redis, context.user_id, context.plan, context.timezone
            ):
                return MESSAGE_LIMIT_TEXT
            if await self._budget_state(context) is BudgetState.SOFT_REFUSE:
                return SOFT_REFUSE_TEXT

            payload = envelope.payload
            # Telegram may omit file_size; the download itself is capped, and the
            # post-download length check below still guards the limit.
            limits = profile_limits()
            max_bytes = limits.max_photo_bytes
            size = payload.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and size > max_bytes:
                return _photo_too_large_text(max_bytes)
            file_id = payload.get("tg_file_id")
            if not isinstance(file_id, str) or not file_id or self._download_file is None:
                return PHOTO_DOWNLOAD_UNAVAILABLE_TEXT
            if not await self._within_monthly_limit(context):
                return MONTHLY_LIMIT_TEXT

            try:
                content = await self._download_file(file_id, max_bytes)
            except Exception:
                self._logger.warning("photo download failed", exc_info=True)
                return PHOTO_DOWNLOAD_UNAVAILABLE_TEXT
            if len(content) > max_bytes:
                return _photo_too_large_text(max_bytes)

            try:
                ocr_text = await asyncio.to_thread(_ocr_photo, content, limits)
            except _OcrUnavailableError:
                return PHOTO_OCR_UNAVAILABLE_TEXT
            except (UnidentifiedImageError, OSError, ValueError):
                return INVALID_PHOTO_TEXT

            await self._increment_monthly_pages(context)
            caption = payload.get("caption")
            caption_text = caption if isinstance(caption, str) else ""
            recognized_text = ocr_text.strip()
            if not caption_text and not recognized_text:
                return EMPTY_PHOTO_TEXT

            text = "Пользователь прислал фото."
            if caption_text:
                text += f"\nПодпись: {caption_text}"
            if recognized_text:
                text += f"\nРаспознанный с фото текст:\n{recognized_text}"
            rewritten = envelope.model_copy(update={"kind": "text", "payload": {"text": text}})
            return await self._inner.process(rewritten, context)
        except Exception:
            self._logger.exception(
                "photo processing failed", extra={"update_id": envelope.update_id}
            )
            return INVALID_PHOTO_TEXT

    async def _within_monthly_limit(self, context: TaskContext) -> bool:
        limit = monthly_pdf_pages_limit()
        if limit is None:
            return True
        raw_count = await self._queue_redis.get(_monthly_pages_key(context))
        raw_text = raw_count.decode("utf-8") if isinstance(raw_count, bytes) else raw_count
        return int(raw_text or "0") + 1 <= limit

    async def _increment_monthly_pages(self, context: TaskContext) -> int:
        key = _monthly_pages_key(context)
        used_pages = await self._queue_redis.incrby(key, 1)
        # Unconditional: setting the TTL only on the first increment leaves the
        # key immortal whenever that single EXPIRE is lost (plan S7).
        await self._queue_redis.expire(key, MONTHLY_KEY_TTL_SECONDS)
        return used_pages

    async def _budget_state(self, context: TaskContext) -> BudgetState:
        spent = await get_spent_rub(self._queue_redis, context.user_id, context.timezone)
        return budget_state(spent, daily_budget_rub(context.plan))


def _ocr_photo(content: bytes, limits: ProfileLimits) -> str:
    """Run Tesseract on image bytes, honoring the local development override."""

    tesseract_cmd = os.getenv("TESSERACT_CMD", "").strip()
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    try:
        with Image.open(BytesIO(content)) as image:
            # A phone photo is the largest single allocation on this path, so it
            # is scaled down before tesseract ever sees it (plan section 2).
            return str(pytesseract.image_to_string(downscaled(image, limits), lang="rus+eng"))
    except (pytesseract.TesseractNotFoundError, FileNotFoundError) as exc:
        raise _OcrUnavailableError from exc


def _photo_too_large_text(max_bytes: int) -> str:
    return PHOTO_TOO_LARGE_TEMPLATE.format(megabytes=max_bytes // (1024 * 1024))


class _OcrUnavailableError(Exception):
    """The optional Tesseract executable cannot be invoked."""
