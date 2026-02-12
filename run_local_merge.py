#!/usr/bin/env python3
"""
Локальное объединение без Telegram: один ZIP или папка с Word → один PDF.
Использование:
  python run_local_merge.py путь/к/архиву.zip
  python run_local_merge.py путь/к/папке
  python run_local_merge.py   (текущая папка)
PDF сохраняется рядом с архивом/папкой с именем merged.pdf (или из имени ZIP).
"""

import sys
import tempfile
from pathlib import Path

from core import (
    get_all_word_files,
    sort_files_by_pages,
    convert_word_to_pdf,
    merge_pdfs,
    process_zip_to_pdf,
)


def process_folder(folder: Path, output_pdf: Path, use_libreoffice: bool = False) -> bool:
    """Папка с .doc/.docx → один PDF."""
    word_files = get_all_word_files(folder)
    if not word_files:
        print("В папке не найдено .doc/.docx")
        return False
    sorted_files = sort_files_by_pages(word_files)
    pdf_dir = folder / "_pdf_temp"
    pdf_dir.mkdir(exist_ok=True)
    pdf_files = []
    try:
        for i, wf in enumerate(sorted_files, 1):
            print(f"Конвертация {i}/{len(sorted_files)}: {wf.name}")
            pdf_path = pdf_dir / (wf.stem + ".pdf")
            if convert_word_to_pdf(wf, pdf_path, use_libreoffice=use_libreoffice) and pdf_path.exists():
                pdf_files.append(pdf_path)
        if not pdf_files:
            print("Ни один файл не конвертирован")
            return False
        ok, total = merge_pdfs(pdf_files, output_pdf)
        if ok:
            print(f"Готово: {output_pdf}, страниц: {total}")
        return ok
    finally:
        for p in pdf_files:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            pdf_dir.rmdir()
        except Exception:
            pass


def main():
    use_libreoffice = __import__("sys").platform != "win32"
    if len(sys.argv) < 2:
        source = Path(".")
    else:
        source = Path(sys.argv[1])
    if not source.exists():
        print(f"Не найден путь: {source}")
        sys.exit(1)
    if source.is_file():
        if source.suffix.lower() != ".zip":
            print("Укажите .zip архив или папку")
            sys.exit(1)
        out_name = source.stem + ".pdf"
        output_pdf = source.parent / out_name
        print(f"Обработка архива: {source}")
        ok, total, failed = process_zip_to_pdf(
            source,
            output_pdf,
            use_libreoffice=use_libreoffice,
            progress_callback=lambda i, n, name: print(f"  {i}/{n}: {name}"),
        )
        if not ok:
            print("Ошибка создания PDF")
            if failed:
                print("Не конвертированы:", failed)
            sys.exit(1)
        print(f"Готово: {output_pdf}, страниц: {total}")
    else:
        output_pdf = source / "merged.pdf"
        print(f"Обработка папки: {source}")
        if not process_folder(source, output_pdf, use_libreoffice=use_libreoffice):
            sys.exit(1)


if __name__ == "__main__":
    main()
