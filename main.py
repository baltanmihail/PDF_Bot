"""
Telegram бот для объединения Word-файлов из ZIP-архива в единый PDF
"""

import os
import sys
import re
import time
import logging
import zipfile
import shutil
import tempfile
from pathlib import Path
from typing import List, Tuple

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

from config import TOKEN
from core import (
    extract_page_number,
    get_all_word_files,
    _logical_filename,
    sort_files_by_pages,
    copy_file_with_retry,
    convert_word_to_pdf,
    merge_pdfs,
)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Временная папка для обработки файлов
TEMP_DIR = tempfile.mkdtemp(prefix='pdf_bot_')

# Отдельная корневая папка для хранения объединяемых файлов пользователей
USER_FILES_ROOT = Path(tempfile.gettempdir()) / "pdf_bot_user_files"
USER_FILES_ROOT.mkdir(parents=True, exist_ok=True)


def create_progress_bar(progress: float, length: int = 20) -> str:
    """
    Создает текстовый прогресс-бар
    progress: значение от 0.0 до 1.0
    length: длина прогресс-бара в символах
    """
    filled = int(progress * length)
    bar = "█" * filled + "░" * (length - filled)
    percentage = int(progress * 100)
    return f"[{bar}] {percentage}%"


async def send_progress_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, 
                                status_line: str, current_operation: str, 
                                progress: float, message_id: int = None) -> int:
    """
    Отправляет или обновляет сообщение с прогрессом
    Возвращает ID сообщения для последующих обновлений
    """
    progress_bar = create_progress_bar(progress)
    text = f"{status_line}\n\n{current_operation}\n\n{progress_bar}"
    
    try:
        if message_id:
            # Обновляем существующее сообщение
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text
            )
            return message_id
        else:
            # Отправляем новое сообщение
            message = await context.bot.send_message(chat_id=chat_id, text=text)
            return message.message_id
    except Exception as e:
        # Если сообщение уже удалено или другая ошибка, отправляем новое
        logger.warning(f"Не удалось обновить сообщение: {e}")
        message = await context.bot.send_message(chat_id=chat_id, text=text)
        return message.message_id


def split_pdf_by_size(pdf_path: Path, max_size_mb: float = 45.0, output_dir: Path = None) -> List[Path]:
    """
    Разбивает PDF-файл на части, если он превышает максимальный размер.
    max_size_mb: максимальный размер одной части в MB (по умолчанию 45 MB, чтобы быть в безопасности)
    Возвращает список путей к частям PDF
    """
    from pypdf import PdfReader, PdfWriter
    
    max_size_bytes = max_size_mb * 1024 * 1024
    file_size = pdf_path.stat().st_size
    
    # Если файл меньше лимита, возвращаем его как есть
    if file_size <= max_size_bytes:
        return [pdf_path]
    
    logger.info(f"PDF файл слишком большой ({file_size / (1024*1024):.2f} MB), разбиваю на части...")
    
    # Определяем директорию для сохранения частей
    if output_dir is None:
        output_dir = pdf_path.parent
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
    
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)
    
    # Пытаемся определить оптимальное количество страниц на часть
    # Начинаем с предположения, что размер пропорционален количеству страниц
    avg_size_per_page = file_size / total_pages
    pages_per_part = int(max_size_bytes / avg_size_per_page) - 1  # -1 для запаса
    
    # Минимум 10 страниц на часть, максимум 100
    pages_per_part = max(10, min(pages_per_part, 100))
    
    parts = []
    current_part = 1
    start_page = 0
    
    while start_page < total_pages:
        writer = PdfWriter()
        end_page = min(start_page + pages_per_part, total_pages)
        
        # Добавляем страницы
        for page_num in range(start_page, end_page):
            writer.add_page(reader.pages[page_num])
        
        # Сохраняем часть
        part_filename = f"{pdf_path.stem}_часть{current_part}.pdf"
        part_path = output_dir / part_filename
        
        with open(part_path, 'wb') as part_file:
            writer.write(part_file)
        
        part_size = part_path.stat().st_size
        
        # Если часть всё ещё слишком большая, уменьшаем размер части
        if part_size > max_size_bytes:
            logger.warning(f"Часть {current_part} всё ещё слишком большая ({part_size / (1024*1024):.2f} MB), уменьшаю размер части...")
            # Удаляем слишком большую часть
            part_path.unlink()
            # Уменьшаем количество страниц и пробуем снова
            pages_per_part = max(5, pages_per_part // 2)
            continue
        
        parts.append(part_path)
        logger.info(f"Создана часть {current_part}: {part_filename} ({end_page - start_page} страниц, {part_size / (1024*1024):.2f} MB)")
        
        start_page = end_page
        current_part += 1
    
    return parts


async def merge_collected_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Объединяет собранные Word-файлы из всех ZIP-архивов в один PDF"""
    user = update.effective_user
    chat_id = update.message.chat_id
    
    # Получаем собранные файлы
    user_data = context.user_data
    collected_files = user_data.get('collected_word_files', [])
    
    if not collected_files:
        await update.message.reply_text(
            "❌ Нет собранных файлов для объединения.\n"
            "Отправьте ZIP-файлы с Word-документами, затем используйте /merge для объединения."
        )
        return
    
    # Сортируем и убираем дубликаты до показа счётчика
    sorted_files = sort_files_by_pages([Path(f) for f in collected_files])
    logger.info(f"Файлов после сортировки: {len(sorted_files)}")
    
    if not sorted_files:
        await update.message.reply_text("❌ Не удалось отсортировать файлы")
        user_data.pop('collected_word_files', None)
        user_data.pop('zip_count', None)
        user_data.pop('zip_names', None)
        return
    
    total_collected = len(collected_files)
    total_to_merge = len(sorted_files)
    zip_count = user_data.get('zip_count', 0)
    if total_to_merge < total_collected:
        msg = (
            f"📦 Найдено {total_collected} файлов из {zip_count} архивов.\n"
            f"После удаления дубликатов: {total_to_merge} файлов.\n"
            f"Начинаю обработку..."
        )
    else:
        msg = (
            f"📦 Найдено {total_to_merge} файлов из {zip_count} архивов.\n"
            f"Начинаю обработку..."
        )
    await update.message.reply_text(msg)
    
    # Создаем временную директорию
    work_dir = Path(TEMP_DIR) / f"work_{user.id}_merge"
    work_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        total_files = len(sorted_files)
        
        # Отправляем начальное сообщение с прогрессом
        status_line = f"✅ Обработано архивов: {user_data.get('zip_count', 0)}"
        current_operation = "🔄 Начинаю конвертацию в PDF..."
        progress_message_id = await send_progress_message(
            context, chat_id, status_line, current_operation, 0.05, None
        )
        
        # Конвертируем каждый файл в PDF
        pdf_dir = work_dir / "pdfs"
        pdf_dir.mkdir(exist_ok=True)
        pdf_files = []
        failed_files = []
        
        for i, word_file in enumerate(sorted_files, 1):
            pdf_name = word_file.stem + ".pdf"
            pdf_path = pdf_dir / pdf_name
            
            # Логируем в терминал
            print(f"Конвертация {i}/{total_files}: {word_file.name}")
            logger.info(f"Конвертация {i}/{total_files}: {word_file.name}")
            
            # Обновляем сообщение в Telegram
            status_line = f"✅ Обработано архивов: {user_data.get('zip_count', 0)}"
            current_operation = f"🔄 Конвертирую {i}/{total_files}: {word_file.name}"
            progress = 0.05 + (i / total_files) * 0.85
            progress_message_id = await send_progress_message(
                context, chat_id, status_line, current_operation, progress, progress_message_id
            )
            
            if convert_word_to_pdf(word_file, pdf_path):
                if pdf_path.exists():
                    pdf_files.append(pdf_path)
                else:
                    failed_files.append(word_file.name)
                    logger.warning(f"PDF не создан для: {word_file.name}")
            else:
                failed_files.append(word_file.name)
                logger.error(f"Ошибка конвертации: {word_file.name}")
        
        if not pdf_files:
            # Проверяем, почему не удалось конвертировать
            try:
                import win32com.client
                error_message = (
                    "❌ Не удалось конвертировать ни одного файла в PDF.\n\n"
                    "Возможные причины:\n"
                    "• Microsoft Word не установлен\n"
                    "• Файлы повреждены или в неподдерживаемом формате\n"
                    "• Ошибки доступа к файлам\n\n"
                    "Проверьте логи бота для подробной информации."
                )
            except ImportError:
                error_message = (
                    "❌ Не удалось конвертировать ни одного файла в PDF.\n\n"
                    "⚠️ **Модуль win32com не найден!**\n\n"
                    "Для работы бота необходимо установить pywin32:\n\n"
                    "1. Установите pywin32:\n"
                    "   `python -m pip install pywin32`\n\n"
                    "2. Запустите скрипт пост-установки (если нужно):\n"
                    "   `python -m pywin32_postinstall -install`\n\n"
                    "3. Перезапустите бота"
                )
            
            await update.message.reply_text(error_message)
            # Очищаем данные пользователя
            user_data.pop('collected_word_files', None)
            user_data.pop('zip_count', None)
            user_data.pop('zip_names', None)
            return
        
        # Обновляем прогресс - объединение PDF
        status_line = f"✅ Обработано архивов: {user_data.get('zip_count', 0)}"
        current_operation = "📎 Объединяю PDF-файлы..."
        progress_message_id = await send_progress_message(
            context, chat_id, status_line, current_operation, 0.95, progress_message_id
        )
        
        if failed_files:
            await update.message.reply_text(
                f"⚠️ Не удалось конвертировать {len(failed_files)} файлов:\n" +
                "\n".join(failed_files[:5]) +
                ("\n..." if len(failed_files) > 5 else "")
            )
        
        # Формируем название итогового PDF на основе названий исходных ZIP-файлов
        zip_names = user_data.get('zip_names', [])
        if zip_names:
            # Если несколько архивов, объединяем названия
            if len(zip_names) == 1:
                output_name = zip_names[0]
            else:
                # Объединяем первые несколько названий
                output_name = "_".join(zip_names[:3])  # Максимум 3 названия
                if len(zip_names) > 3:
                    output_name += f"_и_еще_{len(zip_names) - 3}"
        else:
            # Fallback на старое название, если названия не сохранились
            output_name = "merged_report"
        
        # Очищаем название от недопустимых символов для имени файла
        output_name = re.sub(r'[<>:"/\\|?*]', '_', output_name)
        output_name = output_name.strip(' .')
        
        # Объединяем PDF-файлы
        output_pdf = work_dir / f"{output_name}.pdf"
        success, total_pages = merge_pdfs(pdf_files, output_pdf)
        
        if not success:
            await update.message.reply_text("❌ Ошибка при объединении PDF-файлов")
            return
        
        # Обновляем прогресс - завершено
        status_line = f"✅ Обработано архивов: {user_data.get('zip_count', 0)}"
        current_operation = "✅ Обработка завершена!"
        progress_message_id = await send_progress_message(
            context, chat_id, status_line, current_operation, 1.0, progress_message_id
        )
        
        # Отправляем результат
        await update.message.reply_text(
            f"✅ Отчет готов!\n\n"
            f"📊 Статистика:\n"
            f"• Обработано архивов: {user_data.get('zip_count', 0)}\n"
            f"• Обработано файлов: {len(sorted_files)}\n"
            f"• Успешно конвертировано: {len(pdf_files)}\n"
            f"• Всего страниц: {total_pages}\n"
            f"• Не удалось обработать: {len(failed_files)}"
        )
        
        # Очищаем данные пользователя после успешной обработки
        user_data.pop('collected_word_files', None)
        user_data.pop('zip_count', None)
        user_data.pop('zip_names', None)
        
        # Отправляем PDF (используем существующую логику с разбиением на части)
        file_size = output_pdf.stat().st_size
        file_size_mb = file_size / (1024 * 1024)
        max_file_size_mb = 45.0
        
        pdf_parts = []
        if file_size_mb > max_file_size_mb:
            await update.message.reply_text(
                f"⚠️ PDF-файл слишком большой ({file_size_mb:.2f} MB > {max_file_size_mb} MB).\n"
                f"Разбиваю на части..."
            )
            pdf_parts = split_pdf_by_size(output_pdf, max_file_size_mb, work_dir)
            await update.message.reply_text(f"📄 Файл разделен на {len(pdf_parts)} частей")
        else:
            pdf_parts = [output_pdf]
        
        # Отправляем все части (используем существующую логику отправки)
        total_parts = len(pdf_parts)
        for part_num, part_path in enumerate(pdf_parts, 1):
            part_size = part_path.stat().st_size
            part_size_mb = part_size / (1024 * 1024)
            
            if total_parts > 1:
                await update.message.reply_text(
                    f"📤 Отправляю часть {part_num}/{total_parts} ({part_size_mb:.2f} MB)..."
                )
                output_basename = output_pdf.stem
                filename = f"{output_basename}_часть{part_num}.pdf"
                caption = f"📄 Объединенный отчет, часть {part_num}/{total_parts} ({part_size_mb:.2f} MB)"
            else:
                await update.message.reply_text(f"📤 Отправляю PDF-файл ({part_size_mb:.2f} MB)...")
                filename = output_pdf.name
                caption = f"📄 Объединенный отчет ({part_size_mb:.2f} MB)"
            
            max_retries = 3
            part_sent = False
            is_file_too_large = False
            
            for attempt in range(max_retries):
                try:
                    with open(part_path, 'rb') as pdf_file:
                        logger.info(f"Отправка PDF файла (часть {part_num}/{total_parts}, размер: {part_size_mb:.2f} MB), попытка {attempt + 1}/{max_retries}")
                        
                        timeout_value = max(300, int(part_size / (1024 * 100)))
                        
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=pdf_file,
                            filename=filename,
                            caption=caption,
                            read_timeout=timeout_value,
                            write_timeout=timeout_value,
                            connect_timeout=60
                        )
                        logger.info(f"Часть {part_num}/{total_parts} успешно отправлена пользователю {user.id}")
                        part_sent = True
                        break
                        
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"Ошибка при отправке PDF части {part_num} (попытка {attempt + 1}/{max_retries}): {error_msg}")
                    
                    is_file_too_large = "file is too big" in error_msg.lower() or "request entity too large" in error_msg.lower()
                    
                    if is_file_too_large:
                        await update.message.reply_text(
                            f"⚠️ Часть {part_num} всё ещё слишком большая. "
                            f"Попытка разбить на меньшие части..."
                        )
                        part_path.unlink()
                        smaller_parts = split_pdf_by_size(output_pdf if part_num == 1 else part_path, max_file_size_mb * 0.7, work_dir)
                        pdf_parts[part_num-1:part_num] = smaller_parts
                        total_parts = len(pdf_parts)
                        await update.message.reply_text(f"📄 Часть разделена на {len(smaller_parts)} подчастей")
                        part_sent = True
                        break
                    
                    is_timeout = "TimedOut" in error_msg or "timeout" in error_msg.lower()
                    
                    if attempt < max_retries - 1:
                        wait_time = (attempt + 1) * 5
                        retry_msg = f"⏳ {'Таймаут при отправке' if is_timeout else 'Ошибка при отправке'} части {part_num}.\n"
                        retry_msg += f"Повторная попытка через {wait_time} сек... (попытка {attempt + 1}/{max_retries})"
                        await update.message.reply_text(retry_msg)
                        
                        import asyncio
                        await asyncio.sleep(wait_time)
                    else:
                        error_detail = "Таймаут при отправке" if is_timeout else "Ошибка при отправке"
                        await update.message.reply_text(
                            f"❌ Не удалось отправить часть {part_num}/{total_parts} после {max_retries} попыток.\n\n"
                            f"Ошибка: {error_detail}\n"
                            f"Размер части: {part_size_mb:.2f} MB"
                        )
                        logger.error(f"Не удалось отправить часть {part_num} после всех попыток. Файл: {part_path}")
            
            if not part_sent and not is_file_too_large:
                logger.warning(f"Часть {part_num} не была отправлена. Путь: {part_path}")
        
        logger.info(f"Отчет успешно создан и отправлен пользователю {user.id}")
        
    except Exception as e:
        logger.error(f"Ошибка при объединении файлов: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Произошла ошибка при объединении файлов:\n{str(e)}"
        )
        # Очищаем данные при ошибке
        user_data.pop('collected_word_files', None)
        user_data.pop('zip_count', None)
    finally:
        # Очищаем временные файлы
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.info(f"Временные файлы удалены: {work_dir}")
        except Exception as e:
            logger.warning(f"Не удалось удалить временные файлы: {e}")


async def process_zip_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает полученный ZIP-файл и добавляет файлы в коллекцию"""
    user = update.effective_user
    
    if not update.message.document:
        await update.message.reply_text("Пожалуйста, отправьте ZIP-файл с отчетом.")
        return
    
    file = update.message.document
    
    # Проверяем, что это ZIP-файл
    if not file.file_name.lower().endswith('.zip'):
        await update.message.reply_text("Пожалуйста, отправьте ZIP-файл (.zip)")
        return
    
    # Проверяем размер файла перед скачиванием
    # Telegram Bot API ограничение: 20 MB для обычных файлов, 50 MB для документов
    max_file_size = 20 * 1024 * 1024  # 20 MB в байтах
    
    if file.file_size and file.file_size > max_file_size:
        file_size_mb = file.file_size / (1024 * 1024)
        await update.message.reply_text(
            f"❌ Файл слишком большой для обработки!\n\n"
            f"Размер файла: {file_size_mb:.2f} MB\n"
            f"Максимальный размер: 20 MB\n\n"
            f"💡 Решения:\n"
            f"• Разбейте архив на несколько частей (каждая < 20 MB)\n"
            f"• Удалите лишние файлы из архива\n"
            f"• Используйте сжатие архива (ZIP с максимальным сжатием)"
        )
        return
    
    chat_id = update.message.chat_id
    progress_message_id = None
    
    # Создаем временную директорию для этого запроса
    work_dir = Path(TEMP_DIR) / f"work_{user.id}_{file.file_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Скачиваем файл с обработкой ошибок
        zip_path = work_dir / file.file_name
        try:
            file_obj = await context.bot.get_file(file.file_id)
            await file_obj.download_to_drive(zip_path)
            logger.info(f"ZIP-файл скачан: {zip_path}")
        except Exception as download_error:
            error_msg = str(download_error)
            logger.error(f"Ошибка при скачивании файла: {error_msg}")
            
            # Проверяем, является ли это ошибкой размера файла
            if "too big" in error_msg.lower() or "file is too big" in error_msg.lower():
                await update.message.reply_text(
                    f"❌ Файл слишком большой для скачивания через Telegram Bot API!\n\n"
                    f"Telegram ограничивает размер файлов, которые боты могут получить:\n"
                    f"• Максимум: 20 MB для обычных файлов\n"
                    f"• Максимум: 50 MB для документов (но не всегда доступно)\n\n"
                    f"💡 Решения:\n"
                    f"1. Разбейте ZIP-архив на несколько частей:\n"
                    f"   - Каждая часть должна быть меньше 20 MB\n"
                    f"   - Отправьте части по очереди\n"
                    f"   - Бот обработает каждую часть отдельно\n\n"
                    f"2. Используйте сжатие архива:\n"
                    f"   - ZIP с максимальным сжатием\n"
                    f"   - 7z или RAR с максимальным сжатием\n\n"
                    f"3. Удалите ненужные файлы из архива\n\n"
                    f"4. Используйте облачное хранилище (Google Drive, Dropbox) и отправьте ссылку"
                )
            else:
                await update.message.reply_text(
                    f"❌ Ошибка при скачивании файла:\n{error_msg}\n\n"
                    f"Попробуйте отправить файл еще раз."
                )
            return
        
        # Распаковываем ZIP
        extract_dir = work_dir / "extracted"
        extract_dir.mkdir(exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        
        logger.info(f"ZIP-файл распакован в: {extract_dir}")
        
        # Находим все Word-файлы рекурсивно (включая подпапки типа "Приложения")
        word_files = get_all_word_files(extract_dir)
        logger.info(f"Найдено Word-файлов в архиве (включая подпапки): {len(word_files)}")
        
        # Логируем структуру для отладки
        for word_file in word_files:
            relative_path = word_file.relative_to(extract_dir)
            logger.info(f"Найден файл: {relative_path} (из папки: {relative_path.parent})")
        
        if not word_files:
            await update.message.reply_text("❌ В архиве не найдено Word-файлов (.doc, .docx)")
            return
        
        # Сохраняем название исходного ZIP-файла для использования в итоговом PDF
        zip_filename_base = Path(file.file_name).stem  # Название без расширения .zip
        
        # Копируем файлы в устойчивую директорию пользователя для последующей обработки
        user_data = context.user_data
        if 'collected_word_files' not in user_data:
            user_data['collected_word_files'] = []
            user_data['zip_count'] = 0
            user_data['zip_names'] = []  # Сохраняем названия ZIP-файлов
        
        # Сохраняем название ZIP-файла
        user_data['zip_names'].append(zip_filename_base)
        
        # Отдельная папка под каждый архив — никогда не перезаписываем существующие файлы
        user_root_dir = USER_FILES_ROOT / str(user.id)
        batch_id = user_data['zip_count']
        user_files_dir = user_root_dir / f"batch_{batch_id}"
        user_files_dir.mkdir(parents=True, exist_ok=True)
        
        # Копируем файлы: уникальное имя = индекс + имя (дубликаты в архиве не перезаписывают)
        copied_files = []
        for i, word_file in enumerate(word_files):
            unique_name = f"{i:04d}_{word_file.name}"
            dest_file = user_files_dir / unique_name
            copy_file_with_retry(word_file, dest_file)
            copied_files.append(str(dest_file))
            logger.info(f"Скопирован файл: {word_file.name} -> {unique_name}")
        
        # Добавляем файлы к коллекции пользователя
        user_data['collected_word_files'].extend(copied_files)
        user_data['zip_count'] += 1
        
        total_files = len(user_data['collected_word_files'])
        zip_count = user_data['zip_count']
        
        await update.message.reply_text(
            f"✅ ZIP-архив обработан!\n\n"
            f"📦 Собрано файлов: {total_files}\n"
            f"📁 Обработано архивов: {zip_count}\n\n"
            f"Отправьте еще ZIP-файлы или используйте команду /merge для объединения всех файлов в один PDF."
        )
        
    except Exception as e:
        logger.error(f"Ошибка при обработке файла: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Произошла ошибка при обработке файла:\n{str(e)}"
        )
    finally:
        # Очищаем временные файлы
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.info(f"Временные файлы удалены: {work_dir}")
        except Exception as e:
            logger.warning(f"Не удалось удалить временные файлы: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    await update.message.reply_text(
        "👋 Привет! Я бот для объединения Word-отчетов в PDF.\n\n"
        "📦 Отправьте мне ZIP-файл с вашим отчетом, и я:\n"
        "1️⃣ Распакую архив\n"
        "2️⃣ Найду все Word-файлы\n"
        "3️⃣ Отсортирую их по страницам\n"
        "4️⃣ Конвертирую в PDF\n"
        "5️⃣ Объединю в один файл\n\n"
        "Отправьте ZIP-файл, чтобы начать!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    user_data = context.user_data
    collected_count = len(user_data.get('collected_word_files', []))
    zip_count = user_data.get('zip_count', 0)
    
    status_info = ""
    if collected_count > 0:
        status_info = f"\n\n📊 Текущий статус:\n• Собрано файлов: {collected_count}\n• Обработано архивов: {zip_count}\n• Используйте /merge для объединения"
    
    await update.message.reply_text(
        "ℹ️ Помощь по использованию бота:\n\n"
        "📦 Работа с несколькими архивами:\n"
        "1. Упакуйте Word-файлы в один или несколько ZIP-архивов\n"
        "2. Отправьте ZIP-файл(ы) боту (по очереди или сразу несколько)\n"
        "3. Используйте команду /merge для объединения всех файлов\n"
        "4. Получите готовый объединенный PDF-файл\n\n"
        "📝 Требования:\n"
        "• Файлы должны быть в формате .doc или .docx\n"
        "• В названиях файлов должны быть указаны номера страниц\n"
        "• Титульный лист должен содержать слово 'титул' в названии\n"
        "• Размер каждого ZIP-архива не должен превышать 20 MB\n\n"
        "🔧 Команды:\n"
        "/start - начать работу (очистить собранные файлы)\n"
        "/merge - объединить все собранные файлы в PDF\n"
        "/status - показать текущий статус\n"
        "/cancel - отменить сбор файлов"
        + status_info
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /status"""
    user_data = context.user_data
    collected_files = user_data.get('collected_word_files', [])
    zip_count = user_data.get('zip_count', 0)
    
    if not collected_files:
        await update.message.reply_text(
            "📊 Статус:\n"
            "Нет собранных файлов.\n\n"
            "Отправьте ZIP-файлы с Word-документами для начала работы."
        )
    else:
        await update.message.reply_text(
            f"📊 Статус:\n\n"
            f"• Собрано файлов: {len(collected_files)}\n"
            f"• Обработано архивов: {zip_count}\n\n"
            f"Используйте команду /merge для объединения всех файлов в один PDF."
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /cancel"""
    user_data = context.user_data
    collected_count = len(user_data.get('collected_word_files', []))
    zip_count = user_data.get('zip_count', 0)
    
    if collected_count > 0:
        user_data.clear()
        await update.message.reply_text(
            f"✅ Сбор файлов отменен.\n"
            f"Удалено {collected_count} файлов из {zip_count} архивов."
        )
    else:
        await update.message.reply_text("Нет активного сбора файлов для отмены.")


def main():
    """Главная функция запуска бота"""
    try:
        # Проверяем версию библиотеки
        try:
            import telegram
            print(f"Версия python-telegram-bot: {telegram.__version__}")
            logger.info(f"Версия python-telegram-bot: {telegram.__version__}")
        except Exception as e:
            print(f"Предупреждение: не удалось определить версию библиотеки: {e}")
        
        # Проверяем токен
        if not TOKEN or TOKEN == "":
            logger.error("Токен не найден! Проверьте config.py")
            print("ОШИБКА: Токен не найден! Проверьте config.py")
            return
        
        # Проверяем формат токена (должен быть вида 123456:ABC-DEF...)
        if ":" not in TOKEN or len(TOKEN) < 20:
            logger.error("Токен имеет неправильный формат!")
            print("ОШИБКА: Токен имеет неправильный формат!")
            print("Токен должен быть вида: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
            return
        
        logger.info(f"Используется токен: {TOKEN[:10]}...")
        
        # Проверяем наличие win32com (для конвертации Word в PDF)
        try:
            import win32com.client
            logger.info("Модуль win32com найден - конвертация Word в PDF будет работать")
        except ImportError:
            logger.warning(
                "⚠️ Модуль win32com не найден!\n"
                "Конвертация Word в PDF не будет работать.\n"
                "Установите pywin32:\n"
                "  python -m pip install pywin32\n"
                "Затем запустите (если нужно):\n"
                "  python -m pywin32_postinstall -install"
            )
            print(
                "\n" + "="*60 + "\n"
                "⚠️ ПРЕДУПРЕЖДЕНИЕ: Модуль win32com не найден!\n"
                "Конвертация Word в PDF не будет работать.\n\n"
                "Установите pywin32:\n"
                "  python -m pip install pywin32\n"
                "Затем (если нужно) запустите:\n"
                "  python -m pywin32_postinstall -install\n"
                "="*60 + "\n"
            )
        
        logger.info("Создание приложения...")
        
        # Создаем приложение
        try:
            import traceback
            print("Попытка создать приложение...")
            application = Application.builder().token(TOKEN).build()
            print("Приложение успешно создано!")
        except Exception as build_error:
            error_type = type(build_error).__name__
            error_msg = str(build_error)
            full_traceback = traceback.format_exc()
            
            logger.error(f"Ошибка при создании приложения: {error_type}: {error_msg}")
            logger.error(f"Полный трейсбек:\n{full_traceback}")
            
            print("\n" + "="*60)
            print(f"ОШИБКА: {error_type}: {error_msg}")
            print("="*60)
            print("\nПолный трейсбек:")
            print(full_traceback)
            print("\nВозможные причины:")
            print("1. Неверный токен бота")
            print("2. Проблемы с подключением к интернету")
            print("3. Несовместимая версия библиотеки python-telegram-bot")
            print("4. Отсутствуют зависимости")
            print("\nПопробуйте:")
            print("- Проверить токен в @BotFather")
            print("- Проверить подключение к интернету")
            print("- Переустановить библиотеку: pip install --upgrade python-telegram-bot")
            print("="*60)
            raise
        
        # Регистрируем обработчики команд
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("merge", merge_collected_files))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(CommandHandler("cancel", cancel_command))
        
        # Обработчик ZIP-файлов (должен быть после команд)
        application.add_handler(MessageHandler(filters.Document.ALL, process_zip_file))
        
        # Запускаем бота
        logger.info("Бот запущен")
        print("Бот успешно запущен! Ожидаю сообщений...")
        
        # Исправление для Python 3.10+ с новым поведением asyncio
        import asyncio
        import sys
        
        # В Python 3.10+ нужно установить event loop policy перед использованием
        if sys.version_info >= (3, 10):
            # Устанавливаем WindowsSelectorEventLoopPolicy для Windows
            if sys.platform == 'win32':
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            # Создаем и устанавливаем event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        # Запускаем polling
        application.run_polling()
        
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске бота: {e}", exc_info=True)
        print(f"ОШИБКА: {e}")
        print("Проверьте:")
        print("1. Правильность токена в config.py")
        print("2. Подключение к интернету")
        print("3. Установлены ли все зависимости (pip install -r requirements.txt)")
        raise


if __name__ == "__main__":
    main()

