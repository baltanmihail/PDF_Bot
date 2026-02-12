"""
Ядро логики объединения Word → PDF.
Используется ботом (main.py) и веб-приложением (web_app.py).
Конвертация: Windows — Microsoft Word (COM), Linux/сервер — LibreOffice.
Для максимального совпадения форматирования с Word на сервере включены
настройки экспорта PDF (качество, шрифты, подавление пустых страниц) и
шрифты Liberation (метрически совместимы с Arial/Times). Идеальное
совпадение «как в Word» даёт только конвертация через Word (Windows).
"""

import os
import re
import sys
import time
import logging
import shutil
import zipfile
import tempfile
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional, Callable

logger = logging.getLogger(__name__)


def extract_page_number(filename: str) -> int:
    """Извлекает номер первой страницы из названия файла."""
    filename_lower = filename.lower()
    page_patterns = [
        r'стр\s*[.,]?\s*(\d+)\s*[-–]\s*\d+',
        r'стр\s*[.,]?\s*(\d+)\s*[-–]',
        r'стр\s*[.,]?\s*(\d+)',
    ]
    for pattern in page_patterns:
        match = re.search(pattern, filename_lower)
        if match:
            page_num = int(match.group(1))
            if 1 <= page_num <= 1000:
                return page_num
    range_pattern = r'^(\d+)\s*[-–]\s*\d+'
    match = re.search(range_pattern, filename)
    if match:
        page_num = int(match.group(1))
        if 1 <= page_num <= 1000:
            return page_num
    general_range = r'стр\s*[.,]?\s*(\d+)\s*[-–]\s*\d+'
    match = re.search(general_range, filename_lower)
    if match:
        page_num = int(match.group(1))
        if 1 <= page_num <= 1000:
            return page_num
    after_str = re.search(r'стр\s*[.,]?\s*(.+)', filename_lower)
    if after_str:
        for m in re.finditer(r'\b(\d+)\b', after_str.group(1)):
            num = int(m.group(1))
            if 2 <= num <= 500:
                return num
    if 'титул' in filename_lower:
        return 1
    return 0


def get_all_word_files(root_dir: Path) -> List[Path]:
    """Рекурсивно находит все .doc/.docx без дубликатов."""
    word_files = []
    seen = set()
    for ext in ('.doc', '.docx'):
        for path in root_dir.rglob(f'*{ext}'):
            key = path.resolve()
            if key not in seen:
                seen.add(key)
                word_files.append(path)
    return word_files


def _logical_filename(name: str) -> str:
    """Убирает префикс NNNN_ из имени."""
    m = re.match(r"^\d+_(.+)$", name)
    return m.group(1) if m else name


def sort_files_by_pages(files: List[Path]) -> List[Path]:
    """Сортирует по страницам, убирает дубликаты по (page_num, logical_name)."""
    files_with_pages = []
    seen_key = set()
    for file_path in files:
        filename = file_path.name
        logical = _logical_filename(filename)
        page_num = extract_page_number(filename)
        key = (page_num, logical)
        if key in seen_key:
            logger.debug("Пропуск дубликата: %s", filename)
            continue
        seen_key.add(key)
        files_with_pages.append((page_num, file_path))
    files_with_pages.sort(key=lambda x: x[0] if x[0] > 0 else 9999)
    return [p for _, p in files_with_pages]


def copy_file_with_retry(src: Path, dst: Path, max_attempts: int = 15, delay: float = 0.5) -> None:
    """Копирует файл через .part (избегает WinError 32)."""
    part = dst.with_name(dst.name + '.part')
    last_error = None
    for attempt in range(max_attempts):
        try:
            shutil.copy2(src, part)
            os.replace(part, dst)
            return
        except PermissionError as e:
            last_error = e
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass
            if attempt < max_attempts - 1:
                time.sleep(delay)
            else:
                break
    try:
        with open(src, 'rb') as f_in:
            with open(part, 'wb') as f_out:
                while True:
                    chunk = f_in.read(1024 * 1024)
                    if not chunk:
                        break
                    f_out.write(chunk)
        os.replace(part, dst)
        return
    except Exception:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        raise last_error


def _convert_word_win(word_path: Path, pdf_path: Path) -> bool:
    """Конвертация через Microsoft Word (Windows COM)."""
    try:
        import win32com.client
        import pythoncom
    except ImportError:
        logger.error("pywin32 не установлен")
        return False
    # COM требует CoInitialize в каждом потоке (веб-приложение вызывает из фонового потока)
    pythoncom.CoInitialize()
    try:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        try:
            doc = word.Documents.Open(
                FileName=str(word_path.absolute()),
                ReadOnly=True,
                AddToRecentFiles=False,
                Visible=False,
            )
            try:
                doc.ExportAsFixedFormat(
                    str(pdf_path.absolute()),
                    17,
                    0,
                )
            except Exception:
                try:
                    doc.ExportAsFixedFormat(
                        OutputFileName=str(pdf_path.absolute()),
                        ExportFormat=17,
                        OptimizeFor=0,
                    )
                except Exception:
                    doc.SaveAs(FileName=str(pdf_path.absolute()), FileFormat=17)
            doc.Close(SaveChanges=False)
            return True
        except Exception as e:
            logger.error("Ошибка конвертации %s: %s", word_path, e)
            try:
                doc.Close()
            except Exception:
                pass
            return False
        finally:
            word.Quit()
    except Exception as e:
        logger.error("Word COM: %s", e)
        return False
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _convert_word_libre(word_path: Path, pdf_path: Path, libreoffice_path: Optional[str] = None) -> bool:
    """Конвертация через LibreOffice с настройками для максимального сохранения форматирования."""
    cmd = libreoffice_path or "libreoffice"
    out_dir = pdf_path.parent
    # Параметры экспорта: макс. качество изображений, без уменьшения разрешения,
    # встраивание шрифтов, подавление пустых страниц — для сохранения полей и разметки
    pdf_filter = (
        "pdf:writer_pdf_Export:"
        '{"Quality":{"type":"long","value":"100"},'
        '"UseLosslessCompression":{"type":"boolean","value":"true"},'
        '"ReduceImageResolution":{"type":"boolean","value":"false"},'
        '"IsSkipEmptyPages":{"type":"boolean","value":"true"},'
        '"EmbedStandardFonts":{"type":"boolean","value":"true"}'
        "}"
    )
    try:
        subprocess.run(
            [
                cmd,
                "--headless",
                "--convert-to", pdf_filter,
                "--outdir", str(out_dir),
                str(word_path.absolute()),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        # LibreOffice создаёт файл с тем же именем и расширением .pdf
        expected = out_dir / (word_path.stem + ".pdf")
        if expected.exists():
            if expected != pdf_path:
                shutil.move(str(expected), str(pdf_path))
            return True
        return False
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.error("LibreOffice: %s", e)
        return False


def convert_word_to_pdf(word_path: Path, pdf_path: Path, use_libreoffice: bool = False) -> bool:
    """
    Конвертирует Word в PDF.
    Windows по умолчанию — Word (COM); иначе или use_libreoffice=True — LibreOffice.
    """
    if use_libreoffice or sys.platform != "win32":
        return _convert_word_libre(word_path, pdf_path)
    return _convert_word_win(word_path, pdf_path)


def merge_pdfs(pdf_files: List[Path], output_path: Path) -> Tuple[bool, int]:
    """Объединяет PDF-файлы в один."""
    try:
        from pypdf import PdfWriter, PdfReader
        writer = PdfWriter()
        total_pages = 0
        for pdf_path in pdf_files:
            if not pdf_path.exists():
                continue
            reader = PdfReader(str(pdf_path), strict=False)
            n = len(reader.pages)
            total_pages += n
            for i in range(n):
                writer.add_page(reader.pages[i])
        writer.write(str(output_path))
        return True, total_pages
    except Exception as e:
        logger.error("merge_pdfs: %s", e, exc_info=True)
        return False, 0


def _process_folder_to_pdf_impl(
    work_dir: Path,
    output_pdf_path: Path,
    use_libreoffice: bool,
    progress_callback: Optional[Callable[[int, int, str], None]],
) -> Tuple[bool, int, List[str]]:
    """Общая логика: папка work_dir с .doc/.docx → один PDF."""
    failed = []
    word_files = get_all_word_files(work_dir)
    if not word_files:
        return False, 0, []
    sorted_files = sort_files_by_pages(word_files)
    total = len(sorted_files)
    if progress_callback:
        progress_callback(0, total, "")  # "найдено total файлов"
    pdf_dir = work_dir / "pdfs"
    pdf_dir.mkdir(exist_ok=True)
    pdf_files = []
    for i, wf in enumerate(sorted_files, 1):
        if progress_callback:
            progress_callback(i, total, wf.name)
        pdf_path = pdf_dir / (wf.stem + ".pdf")
        if convert_word_to_pdf(wf, pdf_path, use_libreoffice=use_libreoffice):
            if pdf_path.exists():
                pdf_files.append(pdf_path)
            else:
                failed.append(wf.name)
        else:
            failed.append(wf.name)
    if not pdf_files:
        return False, 0, failed
    if progress_callback:
        progress_callback(-1, total, "merge")  # этап "объединение"
    ok, total_pages = merge_pdfs(pdf_files, output_pdf_path)
    return ok, total_pages, failed


def prepare_zip_for_preview(zip_path: Path) -> Tuple[Path, List[Path]]:
    """Распаковывает ZIP и возвращает (work_dir, sorted_files). Конвертацию не выполняет."""
    work_dir = Path(tempfile.mkdtemp(prefix="pdf_preview_"))
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(work_dir)
    word_files = get_all_word_files(work_dir)
    if not word_files:
        return work_dir, []
    sorted_files = sort_files_by_pages(word_files)
    return work_dir, sorted_files


def process_zip_to_pdf(
    zip_path: Path,
    output_pdf_path: Path,
    use_libreoffice: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[bool, int, List[str]]:
    """
    Распаковывает ZIP, находит Word-файлы, сортирует, конвертирует в PDF, объединяет.
    progress_callback(current, total, filename): current=0 — найдено total файлов;
    current=1..total — конвертация; current=-1 — объединение PDF.
    """
    work_dir = Path(tempfile.mkdtemp(prefix="pdf_merge_"))
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(work_dir)
        return _process_folder_to_pdf_impl(
            work_dir, output_pdf_path, use_libreoffice, progress_callback
        )
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


def process_folder_to_pdf(
    folder_path: Path,
    output_pdf_path: Path,
    use_libreoffice: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[bool, int, List[str]]:
    """
    Обрабатывает папку с .doc/.docx (без ZIP).
    progress_callback — как в process_zip_to_pdf.
    """
    return _process_folder_to_pdf_impl(
        folder_path, output_pdf_path, use_libreoffice, progress_callback
    )


def process_from_file_list(
    work_dir: Path,
    file_paths: List[Path],
    output_pdf_path: Path,
    use_libreoffice: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[bool, int, List[str]]:
    """
    Конвертирует и объединяет в PDF только указанные файлы в заданном порядке.
    work_dir — каталог для временных PDF; file_paths — список путей к .doc/.docx.
    """
    if not file_paths:
        return False, 0, []
    total = len(file_paths)
    if progress_callback:
        progress_callback(0, total, "")
    pdf_dir = work_dir / "pdfs"
    pdf_dir.mkdir(exist_ok=True)
    pdf_files = []
    failed = []
    for i, wf in enumerate(file_paths, 1):
        if progress_callback:
            progress_callback(i, total, wf.name)
        if not wf.exists():
            failed.append(wf.name)
            continue
        pdf_path = pdf_dir / (wf.stem + ".pdf")
        if convert_word_to_pdf(wf, pdf_path, use_libreoffice=use_libreoffice):
            if pdf_path.exists():
                pdf_files.append(pdf_path)
            else:
                failed.append(wf.name)
        else:
            failed.append(wf.name)
    if not pdf_files:
        return False, 0, failed
    if progress_callback:
        progress_callback(-1, total, "merge")
    ok, total_pages = merge_pdfs(pdf_files, output_pdf_path)
    return ok, total_pages, failed
