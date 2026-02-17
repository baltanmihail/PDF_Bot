"""
Ядро логики объединения Word → PDF.
Используется ботом (main.py) и веб-приложением (web_app.py).
Конвертация:
- Windows — Microsoft Word (COM), точное форматирование.
- Linux/сервер: при заданных MS_GRAPH_* — Microsoft Graph (Word в облаке, точная вёрстка);
  иначе LibreOffice (настройки и шрифты Liberation для лучшего совпадения).
См. DEPLOY.md, раздел 6a — настройка Graph для печати.
"""

import os
import re
import sys
import threading
import time
import uuid
import logging
import shutil
import zipfile
import tempfile
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional, Callable

# На Windows Word COM не поддерживает параллельные вызовы — одна конвертация за раз
_word_com_lock = threading.Lock()

# Для Microsoft Graph (опционально)
import urllib.request
import urllib.error
import urllib.parse

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
    """Конвертация через Microsoft Word (Windows COM). Один поток за раз — COM не поддерживает параллельность."""
    try:
        import win32com.client
        import pythoncom
    except ImportError:
        logger.error("pywin32 не установлен")
        return False
    with _word_com_lock:
        pythoncom.CoInitialize()
        try:
            word = win32com.client.Dispatch("Word.Application")
            try:
                word.Visible = False
            except Exception:
                pass  # если Word уже запущен, Visible может быть недоступен — продолжаем без скрытия
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


def _graph_configured() -> bool:
    """Проверяет, заданы ли переменные для конвертации через Microsoft Graph.
    Два режима: 1) рабочий аккаунт — tenant + user_id + client_id + secret;
    2) личный аккаунт — refresh_token + client_id + secret.
    """
    cid = os.environ.get("MS_GRAPH_CLIENT_ID")
    secret = os.environ.get("MS_GRAPH_CLIENT_SECRET")
    if not cid or not secret:
        return False
    if os.environ.get("MS_GRAPH_REFRESH_TOKEN"):
        return True
    return bool(os.environ.get("MS_GRAPH_TENANT_ID") and os.environ.get("MS_GRAPH_USER_ID"))


def _get_graph_token() -> Optional[str]:
    """Получает access token: по refresh_token (личный аккаунт) или client credentials (рабочий)."""
    import json
    client_id = os.environ.get("MS_GRAPH_CLIENT_ID")
    client_secret = os.environ.get("MS_GRAPH_CLIENT_SECRET")
    refresh = os.environ.get("MS_GRAPH_REFRESH_TOKEN", "").strip()
    if refresh and client_id and client_secret:
        url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                j = json.loads(resp.read().decode())
                return j.get("access_token")
        except urllib.error.HTTPError as e:
            logger.error("Graph refresh token error: %s %s", e.code, e.read().decode()[:200])
            return None
        except Exception as e:
            logger.error("Graph refresh token: %s", e)
            return None
    tenant = os.environ.get("MS_GRAPH_TENANT_ID")
    if not all((tenant, client_id, client_secret)):
        return None
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            j = json.loads(resp.read().decode())
            return j.get("access_token")
    except urllib.error.HTTPError as e:
        logger.error("Graph token error: %s %s", e.code, e.read().decode()[:200])
        return None
    except Exception as e:
        logger.error("Graph token: %s", e)
        return None


def _graph_drive_base() -> str:
    """Базовый URL для OneDrive: /me/drive (личный) или /users/{id}/drive (рабочий)."""
    if os.environ.get("MS_GRAPH_REFRESH_TOKEN"):
        return "https://graph.microsoft.com/v1.0/me/drive/root"
    user_id = os.environ.get("MS_GRAPH_USER_ID", "").strip()
    return f"https://graph.microsoft.com/v1.0/users/{user_id}/drive/root"


def _convert_word_graph(word_path: Path, pdf_path: Path) -> bool:
    """
    Конвертация через Microsoft Graph (Word в облаке).
    Поддерживает личный аккаунт (refresh_token) и рабочий (tenant + user_id).
    Файл загружается во временную папку OneDrive, конвертируется в PDF, скачивается.
    """
    token = _get_graph_token()
    if not token:
        return False
    ext = word_path.suffix.lower()
    if ext not in (".doc", ".docx"):
        return False
    safe_name = f"AppTemp/{uuid.uuid4().hex}{ext}"
    drive_base = _graph_drive_base()
    base = f"{drive_base}:/{safe_name}"
    headers = {"Authorization": f"Bearer {token}"}
    # 1) Загрузка файла
    with open(word_path, "rb") as f:
        body = f.read()
    req = urllib.request.Request(
        f"{base}:/content",
        data=body,
        method="PUT",
        headers={**headers, "Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status not in (200, 201):
                return False
    except urllib.error.HTTPError as e:
        logger.error("Graph upload: %s %s", e.code, e.read().decode()[:300])
        return False
    except Exception as e:
        logger.error("Graph upload: %s", e)
        return False
    # 2) Запрос PDF (Graph возвращает 302 на предподписанный URL; urllib следует редиректу)
    req2 = urllib.request.Request(
        f"{base}:/content?format=pdf",
        method="GET",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req2, timeout=120) as resp:
            pdf_path.write_bytes(resp.read())
    except urllib.error.HTTPError as e:
        logger.error("Graph convert: %s %s", e.code, e.read().decode()[:300])
        try:
            del_req = urllib.request.Request(f"{base}", method="DELETE", headers=headers)
            urllib.request.urlopen(del_req, timeout=10)
        except Exception:
            pass
        return False
    except Exception as e:
        logger.error("Graph convert: %s", e)
        try:
            del_req = urllib.request.Request(f"{base}", method="DELETE", headers=headers)
            urllib.request.urlopen(del_req, timeout=10)
        except Exception:
            pass
        return False
    # 3) Удаление временного файла
    try:
        del_req = urllib.request.Request(f"{base}", method="DELETE", headers=headers)
        urllib.request.urlopen(del_req, timeout=10)
    except Exception:
        pass
    return True


def convert_word_to_pdf(word_path: Path, pdf_path: Path, use_libreoffice: bool = False) -> bool:
    """
    Конвертирует Word в PDF.
    Windows — Microsoft Word (COM). Linux/сервер: при наличии MS_GRAPH_* используется
    Microsoft Graph (Word в облаке, точное форматирование); иначе LibreOffice.
    """
    if sys.platform == "win32" and not use_libreoffice:
        return _convert_word_win(word_path, pdf_path)
    if _graph_configured():
        if _convert_word_graph(word_path, pdf_path):
            return True
        logger.warning("Graph conversion failed, falling back to LibreOffice")
    return _convert_word_libre(word_path, pdf_path)


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
