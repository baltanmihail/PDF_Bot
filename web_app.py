"""
Веб-приложение: ZIP или папка → объединённый PDF. Аккаунты (логин = часть email до @), «Мои файлы».
Запуск: uvicorn web_app:app --host 0.0.0.0 --port 8000
"""

import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from threading import Thread
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Request, Depends
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from core import (
    process_zip_to_pdf,
    process_folder_to_pdf,
    process_from_file_list,
    prepare_zip_for_preview,
    get_all_word_files,
    sort_files_by_pages,
    extract_page_number,
)
from auth_db import (
    init_db,
    create_user,
    auth_user,
    create_session,
    read_session,
    save_job,
    get_job,
    get_user_jobs,
    get_user_storage_dir,
)

# Логи в память для страницы /logs (последние 200 строк)
LOG_LINES: list = []
MAX_LOG_LINES = 200


class MemoryHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            LOG_LINES.append(msg)
            if len(LOG_LINES) > MAX_LOG_LINES:
                LOG_LINES.pop(0)
        except Exception:
            pass


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
mem_handler = MemoryHandler()
mem_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(mem_handler)
logging.getLogger().addHandler(mem_handler)  # логи из core и др. модулей

USE_LIBREOFFICE = __import__("sys").platform != "win32"

app = FastAPI(title="Word → PDF Merge")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

init_db()

# job_id -> {stage, total, current, ..., user_id?, pdf_path, filename}
jobs: Dict[str, Dict[str, Any]] = {}

LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Вход — Word в PDF</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: system-ui, sans-serif; max-width: 360px; margin: 3rem auto; padding: 0 1rem; }
        h1 { font-size: 1.35rem; }
        .form-group { margin-bottom: 1rem; }
        .form-group label { display: block; margin-bottom: 0.35rem; color: #555; }
        .form-group input { width: 100%; padding: 0.5rem; border: 1px solid #ccc; border-radius: 8px; }
        button.primary { width: 100%; padding: 0.6rem; background: #0d6efd; color: white; border: none; border-radius: 8px; font-size: 1rem; cursor: pointer; }
        .tabs { display: flex; margin-bottom: 1rem; }
        .tabs button { flex: 1; padding: 0.5rem; border: 1px solid #ccc; background: #f5f5f5; cursor: pointer; }
        .tabs button.active { background: #0d6efd; color: white; border-color: #0d6efd; }
        #regForm { display: none; }
        .err { color: #c00; font-size: 0.9rem; margin-top: 0.5rem; }
    </style>
</head>
<body>
    <h1>Объединение Word в PDF</h1>
    <p style="color:#666; margin-bottom: 1.5rem;">Логин — часть email до @ (например <code>ivan</code> из ivan@gmail.com). Пароль задаёте сами.</p>
    <div class="tabs">
        <button type="button" id="tabLogin" class="active">Вход</button>
        <button type="button" id="tabReg">Регистрация</button>
    </div>
    <form id="loginForm" method="post" action="/login">
        <div class="form-group">
            <label>Логин</label>
            <input type="text" name="username" placeholder="ivan" required autocomplete="username">
        </div>
        <div class="form-group">
            <label>Пароль</label>
            <input type="password" name="password" placeholder="••••••" required autocomplete="current-password">
        </div>
        <p id="loginErr" class="err"></p>
        <button type="submit" class="primary">Войти</button>
    </form>
    <form id="regForm" method="post" action="/register" style="display:none;">
        <div class="form-group">
            <label>Логин (часть email до @)</label>
            <input type="text" name="username" placeholder="ivan" required autocomplete="username">
        </div>
        <div class="form-group">
            <label>Пароль (не менее 4 символов)</label>
            <input type="password" name="password" placeholder="••••••" required autocomplete="new-password">
        </div>
        <p id="regErr" class="err"></p>
        <button type="submit" class="primary">Зарегистрироваться</button>
    </form>
    <script>
        document.getElementById('tabLogin').onclick = function() { document.getElementById('loginForm').style.display = 'block'; document.getElementById('regForm').style.display = 'none'; this.classList.add('active'); document.getElementById('tabReg').classList.remove('active'); };
        document.getElementById('tabReg').onclick = function() { document.getElementById('regForm').style.display = 'block'; document.getElementById('loginForm').style.display = 'none'; this.classList.add('active'); document.getElementById('tabLogin').classList.remove('active'); };
        document.getElementById('loginForm').onsubmit = function(e) { e.preventDefault(); var fd = new FormData(this); fetch('/login', { method: 'POST', body: fd }).then(r => r.json()).then(d => { if (d.ok) window.location = '/'; else document.getElementById('loginErr').textContent = d.error || 'Ошибка'; }).catch(() => document.getElementById('loginErr').textContent = 'Ошибка сети'); };
        document.getElementById('regForm').onsubmit = function(e) { e.preventDefault(); var fd = new FormData(this); fetch('/register', { method: 'POST', body: fd }).then(r => r.json()).then(d => { if (d.ok) window.location = '/'; else document.getElementById('regErr').textContent = d.error || 'Ошибка'; }).catch(() => document.getElementById('regErr').textContent = 'Ошибка сети'); });
    </script>
</body>
</html>
"""


def get_current_user_id(request: Request) -> Optional[int]:
    session = request.cookies.get("session")
    if not session:
        return None
    return read_session(session)


def _run_job(job_id: str, zip_path: Optional[Path], folder_path: Optional[Path], out_path: Path, out_filename: str):
    try:
        def cb(current: int, total: int, name: str):
            jobs[job_id]["total"] = total
            jobs[job_id]["current"] = current
            jobs[job_id]["current_file"] = name
            if name and name != "merge":
                jobs[job_id]["file_names"].append(name)
            if current == 0:
                jobs[job_id]["stage"] = "found"
            elif current == -1:
                jobs[job_id]["stage"] = "merge"
            else:
                jobs[job_id]["stage"] = "convert"

        if zip_path is not None:
            ok, total_pages, failed = process_zip_to_pdf(
                zip_path, out_path, use_libreoffice=USE_LIBREOFFICE, progress_callback=cb
            )
        else:
            ok, total_pages, failed = process_folder_to_pdf(
                folder_path, out_path, use_libreoffice=USE_LIBREOFFICE, progress_callback=cb
            )

        if not ok:
            jobs[job_id]["stage"] = "error"
            jobs[job_id]["error"] = "Не удалось создать PDF." + (" Не конвертированы: " + ", ".join(failed[:5]) if failed else "")
            return
        user_id = jobs[job_id].get("user_id")
        if user_id:
            dest_dir = get_user_storage_dir(user_id)
            safe_name = out_filename
            dest = dest_dir / safe_name
            if dest.exists():
                dest = dest_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
            shutil.copy2(out_path, dest)
            save_job(user_id, job_id, dest.name, str(dest), total_pages)
            jobs[job_id]["pdf_path"] = str(dest)
        else:
            jobs[job_id]["pdf_path"] = str(out_path)
        jobs[job_id]["stage"] = "done"
        jobs[job_id]["total_pages"] = total_pages
        jobs[job_id]["filename"] = (dest.name if user_id else out_filename)
        jobs[job_id]["done"] = True
    except Exception as e:
        logger.exception("Job %s failed", job_id)
        jobs[job_id]["stage"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["done"] = True


def _run_job_from_preview(job_id: str, work_dir: Path, order: List[int], sorted_paths: List[str], out_filename: str, user_id: int):
    """Конвертация по выбранному пользователем порядку (после превью)."""
    try:
        file_list = [Path(sorted_paths[i]) for i in order if 0 <= i < len(sorted_paths)]
        if not file_list:
            jobs[job_id]["stage"] = "error"
            jobs[job_id]["error"] = "Нет файлов для конвертации"
            jobs[job_id]["done"] = True
            return
        out_path = work_dir / "merged.pdf"
        jobs[job_id]["stage"] = "processing"
        jobs[job_id]["total"] = len(file_list)
        jobs[job_id]["current"] = 0
        jobs[job_id]["file_names"] = []

        def cb(current: int, total: int, name: str):
            jobs[job_id]["total"] = total
            jobs[job_id]["current"] = current
            jobs[job_id]["current_file"] = name
            if name and name != "merge":
                jobs[job_id]["file_names"].append(name)
            if current == 0:
                jobs[job_id]["stage"] = "found"
            elif current == -1:
                jobs[job_id]["stage"] = "merge"
            else:
                jobs[job_id]["stage"] = "convert"

        ok, total_pages, failed = process_from_file_list(
            work_dir, file_list, out_path, use_libreoffice=USE_LIBREOFFICE, progress_callback=cb
        )
        if not ok:
            jobs[job_id]["stage"] = "error"
            jobs[job_id]["error"] = "Не удалось создать PDF." + (" Не конвертированы: " + ", ".join(failed[:5]) if failed else "")
            return
        dest_dir = get_user_storage_dir(user_id)
        safe_name = out_filename
        dest = dest_dir / safe_name
        if dest.exists():
            dest = dest_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        shutil.copy2(out_path, dest)
        save_job(user_id, job_id, dest.name, str(dest), total_pages)
        jobs[job_id]["pdf_path"] = str(dest)
        jobs[job_id]["stage"] = "done"
        jobs[job_id]["total_pages"] = total_pages
        jobs[job_id]["filename"] = dest.name
        jobs[job_id]["done"] = True
    except Exception as e:
        logger.exception("Job %s failed", job_id)
        jobs[job_id]["stage"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["done"] = True


HTML_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Объединение Word в PDF</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: system-ui, -apple-system, sans-serif; max-width: 560px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
        h1 { font-size: 1.35rem; margin-bottom: 0.5rem; }
        .tabs { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
        .tabs button { padding: 0.5rem 1rem; border: 1px solid #ccc; background: #f5f5f5; border-radius: 8px; cursor: pointer; }
        .tabs button.active { background: #0d6efd; color: white; border-color: #0d6efd; }
        .panel { display: none; }
        .panel.active { display: block; }
        .upload-zone { border: 2px dashed #ccc; border-radius: 12px; padding: 2rem; text-align: center; background: #fafafa; margin-bottom: 1rem; }
        .upload-zone.dragover { border-color: #0d6efd; background: #e7f1ff; }
        input[type="file"] { display: block; margin: 0 auto 1rem; }
        button.primary { background: #0d6efd; color: white; border: none; padding: 0.6rem 1.5rem; border-radius: 8px; font-size: 1rem; cursor: pointer; }
        button.primary:hover { background: #0b5ed7; }
        button.primary:disabled { opacity: 0.6; cursor: not-allowed; }
        #progress { display: none; margin-top: 1rem; padding: 1rem; background: #f0f4f8; border-radius: 12px; }
        #progress.visible { display: block; }
        .progress-wrap { display: flex; align-items: center; gap: 0.75rem; margin: 0.5rem 0; }
        .progress-bar { flex: 1; height: 24px; background: #e0e0e0; border-radius: 12px; overflow: hidden; }
        .progress-bar div { height: 100%; background: #0d6efd; transition: width 0.2s; }
        .progress-pct { font-variant-numeric: tabular-nums; font-weight: 600; color: #0d6efd; min-width: 3rem; text-align: right; }
        .stat { margin: 0.25rem 0; }
        .file-list { max-height: 200px; overflow-y: auto; font-size: 0.9rem; margin-top: 0.5rem; }
        .file-list div { padding: 0.2rem 0; border-bottom: 1px solid #eee; }
        .file-list .current { background: #e7f1ff; font-weight: 500; }
        .msg { margin-top: 1rem; padding: 0.75rem; border-radius: 8px; }
        .msg.err { background: #f8d7da; color: #721c24; }
        .msg.ok { background: #d1e7dd; color: #0f5132; }
        .logs-hint { font-size: 0.85rem; color: #666; margin-top: 1.5rem; }
        .logs-hint a { color: #0d6efd; }
        #preview { display: none; margin-top: 1rem; padding: 1rem; background: #f0f4f8; border-radius: 12px; }
        #preview.visible { display: block; }
        .preview-title { font-weight: 600; margin-bottom: 0.5rem; }
        .preview-list { max-height: 280px; overflow-y: auto; margin: 0.5rem 0; }
        .preview-row { display: flex; align-items: center; gap: 0.5rem; padding: 0.35rem 0; border-bottom: 1px solid #e0e0e0; }
        .preview-row .name { flex: 1; font-size: 0.9rem; overflow: hidden; text-overflow: ellipsis; }
        .preview-row .page { color: #666; font-size: 0.85rem; min-width: 3rem; }
        .preview-row button { padding: 0.2rem 0.5rem; font-size: 0.85rem; cursor: pointer; border: 1px solid #ccc; border-radius: 4px; background: #fff; }
        .preview-row button:hover { background: #eee; }
        .preview-row button.del { color: #c00; border-color: #fcc; }
        .btn-convert { margin-top: 0.75rem; }
    </style>
</head>
<body>
    <h1>Объединение Word в PDF</h1>
    <p>Загрузите ZIP или папку с .doc/.docx. Проверьте порядок файлов при необходимости и начните конвертацию. <strong>Несколько вкладок</strong> — можно обрабатывать разные файлы параллельно.</p>

    <div class="tabs">
        <button type="button" id="tabZip" class="active">ZIP-архив</button>
        <button type="button" id="tabFolder">Папка</button>
    </div>

    <div id="panelZip" class="panel active">
        <div class="upload-zone" id="dropZip">
            <input type="file" id="fileZip" name="file" accept=".zip">
            <p>или перетащите ZIP-файл сюда</p>
            <button type="button" class="primary" id="btnZip">Объединить в PDF</button>
        </div>
    </div>

    <div id="panelFolder" class="panel">
        <div class="upload-zone" id="dropFolder">
            <input type="file" id="fileFolder" name="files" webkitdirectory directory multiple>
            <p>Выберите папку (все .doc/.docx внутри будут обработаны)</p>
            <button type="button" class="primary" id="btnFolder">Объединить в PDF</button>
        </div>
    </div>

    <div id="preview">
        <div class="preview-title">Проверьте порядок файлов (при ошибке в названии переместите или удалите лишние)</div>
        <div class="preview-list" id="previewList"></div>
        <button type="button" class="primary btn-convert" id="btnStartConvert">Начать конвертацию</button>
    </div>

    <div id="progress">
        <div class="stat" id="statStage">Подготовка…</div>
        <div class="progress-wrap">
            <div class="progress-bar"><div id="progressBar" style="width: 0%"></div></div>
            <span class="progress-pct" id="progressPct">0%</span>
        </div>
        <div class="stat" id="statDetail"></div>
        <div class="file-list" id="fileList"></div>
        <div class="stat" id="statPages" style="margin-top: 0.5rem;"></div>
        <p id="downloadLink" style="display:none; margin-top: 0.5rem;"></p>
        <p id="errorMsg" class="msg err" style="display:none"></p>
    </div>

    <section id="myFiles" style="margin-top: 2rem;">
        <h2 style="font-size: 1.1rem;">Мои файлы</h2>
        <p id="myFilesList" class="stat">Загрузка…</p>
        <p id="myFilesNone" style="display:none; color: #666;">Пока нет готовых PDF. Загрузите ZIP или папку выше.</p>
    </section>

    <p class="logs-hint">Логи: <a href="/logs" target="_blank">/logs</a>. Вы вошли как <span id="userName"></span> · <a href="/logout">Выйти</a></p>

    <script>
        const tabZip = document.getElementById('tabZip');
        const tabFolder = document.getElementById('tabFolder');
        const panelZip = document.getElementById('panelZip');
        const panelFolder = document.getElementById('panelFolder');
        tabZip.onclick = () => { tabZip.classList.add('active'); tabFolder.classList.remove('active'); panelZip.classList.add('active'); panelFolder.classList.remove('active'); };
        tabFolder.onclick = () => { tabFolder.classList.add('active'); tabZip.classList.remove('active'); panelFolder.classList.add('active'); panelZip.classList.remove('active'); };

        function showProgress(visible) {
            document.getElementById('progress').classList.toggle('visible', visible);
            document.getElementById('errorMsg').style.display = 'none';
            document.getElementById('downloadLink').innerHTML = '';
        }

        function setProgress(pct, stage, detail, fileNames, currentFile, totalPages) {
            var p = Math.round(pct || 0);
            document.getElementById('progressBar').style.width = p + '%';
            document.getElementById('progressPct').textContent = p + '%';
            document.getElementById('statStage').textContent = stage || '';
            document.getElementById('statDetail').textContent = detail || '';
            const list = document.getElementById('fileList');
            if (fileNames && fileNames.length) {
                list.innerHTML = fileNames.map(f => '<div class="' + (f === currentFile ? 'current' : '') + '">' + escapeHtml(f) + '</div>').join('');
                if (currentFile) list.querySelector('.current') && list.querySelector('.current').scrollIntoView({ block: 'nearest' });
            }
            document.getElementById('statPages').textContent = totalPages ? 'Всего страниц в PDF: ' + totalPages : '';
        }

        function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

        function poll(jobId) {
            const f = () => fetch('/progress/' + jobId).then(r => r.json()).then(data => {
                const total = data.total || 0;
                const current = data.current || 0;
                let pct = 0;
                let stage = 'Обработка…';
                if (data.stage === 'found') {
                    stage = 'Найдено файлов: ' + total;
                    setProgress(5, stage, '', data.file_names, null, null);
                } else if (data.stage === 'convert') {
                    pct = 5 + (current / total) * 85;
                    stage = 'Конвертация ' + current + '/' + total;
                    setProgress(pct, stage, data.current_file || '', data.file_names, data.current_file, null);
                } else if (data.stage === 'merge') {
                    stage = 'Объединение PDF…';
                    setProgress(92, stage, '', data.file_names, null, null);
                } else if (data.stage === 'done') {
                    setProgress(100, 'Готово', '', data.file_names, null, data.total_pages);
                    document.getElementById('downloadLink').style.display = 'block';
                    document.getElementById('downloadLink').innerHTML = '<a class="primary" href="/download/' + jobId + '" style="display:inline-block;padding:0.5rem 1rem;text-decoration:none;border-radius:8px;">Скачать PDF</a>';
                    document.getElementById('btnZip').disabled = false;
                    document.getElementById('btnFolder').disabled = false;
                    return;
                } else if (data.stage === 'error') {
                    document.getElementById('errorMsg').textContent = data.error || 'Ошибка';
                    document.getElementById('errorMsg').style.display = 'block';
                    document.getElementById('btnZip').disabled = false;
                    document.getElementById('btnFolder').disabled = false;
                    return;
                }
                setTimeout(f, 400);
            }).catch(() => setTimeout(f, 1000));
            f();
        }

        var currentPreviewJobId = null;
        var currentPreviewFiles = [];

        function renderPreviewList() {
            var list = document.getElementById('previewList');
            list.innerHTML = currentPreviewFiles.map((f, idx) =>
                '<div class="preview-row" data-idx="' + idx + '">' +
                '<button type="button" class="up" title="Выше">↑</button>' +
                '<button type="button" class="down" title="Ниже">↓</button>' +
                '<span class="name">' + escapeHtml(f.name) + '</span>' +
                '<span class="page">' + (f.page ? 'стр. ' + f.page : '') + '</span>' +
                '<button type="button" class="del" title="Удалить">✕</button></div>'
            ).join('');
            list.querySelectorAll('.up').forEach(function(btn) {
                btn.onclick = function() { var idx = parseInt(btn.closest('.preview-row').dataset.idx, 10); if (idx > 0) { var t = currentPreviewFiles[idx]; currentPreviewFiles[idx] = currentPreviewFiles[idx-1]; currentPreviewFiles[idx-1] = t; renderPreviewList(); } };
            });
            list.querySelectorAll('.down').forEach(function(btn) {
                btn.onclick = function() { var idx = parseInt(btn.closest('.preview-row').dataset.idx, 10); if (idx < currentPreviewFiles.length - 1) { var t = currentPreviewFiles[idx]; currentPreviewFiles[idx] = currentPreviewFiles[idx+1]; currentPreviewFiles[idx+1] = t; renderPreviewList(); } };
            });
            list.querySelectorAll('.del').forEach(function(btn) {
                btn.onclick = function() { var idx = parseInt(btn.closest('.preview-row').dataset.idx, 10); currentPreviewFiles.splice(idx, 1); renderPreviewList(); };
            });
        }

        function showPreview(jobId, files) {
            currentPreviewJobId = jobId;
            currentPreviewFiles = files.slice();
            document.getElementById('progress').classList.remove('visible');
            document.getElementById('preview').classList.add('visible');
            document.getElementById('errorMsg').style.display = 'none';
            renderPreviewList();
            document.getElementById('btnZip').disabled = false;
            document.getElementById('btnFolder').disabled = false;
        }

        function upload(formData, isFolder) {
            document.getElementById('preview').classList.remove('visible');
            showProgress(true);
            setProgress(0, 'Загрузка…', '', [], null, null);
            document.getElementById('btnZip').disabled = true;
            document.getElementById('btnFolder').disabled = true;
            fetch(isFolder ? '/upload-folder' : '/upload', { method: 'POST', body: formData })
                .then(r => r.json())
                .then(data => {
                    if (data.detail) { document.getElementById('errorMsg').textContent = data.detail; document.getElementById('errorMsg').style.display = 'block'; document.getElementById('btnZip').disabled = false; document.getElementById('btnFolder').disabled = false; return; }
                    if (data.stage === 'preview' && data.files && data.files.length) {
                        showPreview(data.job_id, data.files);
                        return;
                    }
                    if (data.job_id) poll(data.job_id);
                    else { document.getElementById('errorMsg').textContent = data.detail || 'Ошибка'; document.getElementById('errorMsg').style.display = 'block'; document.getElementById('btnZip').disabled = false; document.getElementById('btnFolder').disabled = false; }
                })
                .catch(e => {
                    document.getElementById('errorMsg').textContent = e.message || 'Ошибка сети';
                    document.getElementById('errorMsg').style.display = 'block';
                    document.getElementById('btnZip').disabled = false;
                    document.getElementById('btnFolder').disabled = false;
                });
        }

        document.getElementById('btnStartConvert').onclick = function() {
            if (!currentPreviewJobId || !currentPreviewFiles.length) { alert('Нет файлов для конвертации'); return; }
            var order = currentPreviewFiles.map(function(f) { return f.index; });
            document.getElementById('preview').classList.remove('visible');
            showProgress(true);
            setProgress(0, 'Подготовка…', '', [], null, null);
            document.getElementById('btnZip').disabled = true;
            document.getElementById('btnFolder').disabled = true;
            fetch('/convert/' + currentPreviewJobId, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ order: order }) })
                .then(r => r.json())
                .then(data => {
                    if (data.detail) { document.getElementById('errorMsg').textContent = data.detail; document.getElementById('errorMsg').style.display = 'block'; document.getElementById('btnZip').disabled = false; document.getElementById('btnFolder').disabled = false; return; }
                    poll(currentPreviewJobId);
                })
                .catch(e => {
                    document.getElementById('errorMsg').textContent = e.message || 'Ошибка';
                    document.getElementById('errorMsg').style.display = 'block';
                    document.getElementById('btnZip').disabled = false;
                    document.getElementById('btnFolder').disabled = false;
                });
        };

        document.getElementById('btnZip').onclick = () => {
            const input = document.getElementById('fileZip');
            if (!input.files || !input.files[0]) { alert('Выберите ZIP-файл'); return; }
            const fd = new FormData();
            fd.append('file', input.files[0]);
            upload(fd, false);
        };

        document.getElementById('btnFolder').onclick = () => {
            const input = document.getElementById('fileFolder');
            if (!input.files || !input.files.length) { alert('Выберите папку'); return; }
            const fd = new FormData();
            for (let i = 0; i < input.files.length; i++) fd.append('files', input.files[i], input.files[i].webkitRelativePath || input.files[i].name);
            upload(fd, true);
        };

        ['dropZip','dropFolder'].forEach(id => {
            const el = document.getElementById(id);
            ['dragenter','dragover'].forEach(ev => el.addEventListener(ev, e => { e.preventDefault(); el.classList.add('dragover'); }));
            ['dragleave','drop'].forEach(ev => el.addEventListener(ev, e => { e.preventDefault(); el.classList.remove('dragover'); }));
        });
        document.getElementById('dropZip').addEventListener('drop', e => {
            const f = e.dataTransfer.files[0];
            if (f && f.name.toLowerCase().endsWith('.zip')) document.getElementById('fileZip').files = e.dataTransfer.files;
        });

        fetch('/api/me').then(r => r.json()).then(d => { document.getElementById('userName').textContent = d.username || ''; });
        fetch('/api/my-files').then(r => r.json()).then(data => {
            var list = document.getElementById('myFilesList');
            var none = document.getElementById('myFilesNone');
            if (data.detail && data.detail === 'not_authenticated') { list.style.display = 'none'; none.style.display = 'block'; return; }
            if (!data.files || data.files.length === 0) { list.style.display = 'none'; none.style.display = 'block'; return; }
            list.style.display = 'block'; none.style.display = 'none';
            list.innerHTML = data.files.map(f => '<div style="margin:0.35rem 0;"><a href="/download/' + f.job_id + '">' + escapeHtml(f.filename) + '</a>' + (f.total_pages ? ' · ' + f.total_pages + ' стр.' : '') + ' <span style="color:#888;font-size:0.85rem">' + (f.created_at || '') + '</span></div>').join('');
        }).catch(() => { document.getElementById('myFilesNone').style.display = 'block'; document.getElementById('myFilesList').style.display = 'none'; });
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if get_current_user_id(request) is None:
        return HTMLResponse(LOGIN_PAGE)
    return HTML_PAGE


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse(LOGIN_PAGE)


@app.post("/login")
async def login_post(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    user_id = auth_user(username, password)
    if user_id is None:
        return JSONResponse({"ok": False, "error": "Неверный логин или пароль"}, status_code=200)
    token = create_session(user_id)
    r = JSONResponse({"ok": True}, status_code=200)
    r.set_cookie("session", token, max_age=60 * 60 * 24 * 7, httponly=True, samesite="lax")
    return r


@app.post("/register")
async def register_post(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    user_id, err = create_user(username, password)
    if user_id is None:
        return JSONResponse({"ok": False, "error": err}, status_code=200)
    token = create_session(user_id)
    r = JSONResponse({"ok": True}, status_code=200)
    r.set_cookie("session", token, max_age=60 * 60 * 24 * 7, httponly=True, samesite="lax")
    return r


@app.get("/logout")
def logout():
    r = RedirectResponse(url="/login", status_code=302)
    r.delete_cookie("session")
    return r


@app.get("/api/me")
def api_me(request: Request):
    uid = get_current_user_id(request)
    if uid is None:
        return JSONResponse({"username": None}, status_code=200)
    import auth_db as ad
    with ad._db() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
    return JSONResponse({"username": row["username"] if row else None})


@app.get("/api/my-files")
def api_my_files(request: Request):
    uid = get_current_user_id(request)
    if uid is None:
        return JSONResponse({"detail": "not_authenticated", "files": []}, status_code=200)
    files = get_user_jobs(uid)
    return JSONResponse({"files": [{"job_id": f["job_id"], "filename": f["filename"], "total_pages": f.get("total_pages"), "created_at": (f.get("created_at") or "")[:19]} for f in files]})


@app.get("/logs", response_class=HTMLResponse)
def logs_page():
    import html
    lines = LOG_LINES[-MAX_LOG_LINES:]
    body = "<pre style='font-size:12px;white-space:pre-wrap'>" + html.escape("\n".join(lines)) + "</pre>" if lines else "<p>Пока нет записей. Запустите обработку и смотрите также терминал uvicorn.</p>"
    return "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Логи</title></head><body><h2>Последние логи</h2><p><small>Логи также выводятся в терминал, где запущен <code>uvicorn web_app:app</code>.</small></p>" + body + "</body></html>"


def _cleanup(path: Path) -> None:
    try:
        if path.is_dir():
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _make_preview_response(job_id: str, sorted_files: List[Path], out_filename: str, work_dir: Path, user_id: int):
    files = [{"index": i, "name": p.name, "page": extract_page_number(p.name)} for i, p in enumerate(sorted_files)]
    jobs[job_id] = {
        "stage": "preview",
        "work_dir": str(work_dir),
        "sorted_file_paths": [str(p) for p in sorted_files],
        "user_id": user_id,
        "out_filename": out_filename,
        "total": 0, "current": 0, "current_file": "", "file_names": [], "total_pages": 0, "done": False, "error": None, "pdf_path": None, "filename": out_filename,
    }
    return {"job_id": job_id, "stage": "preview", "files": files}


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    user_id = get_current_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Войдите в аккаунт")
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Нужен ZIP-файл")
    job_id = str(uuid.uuid4())
    suffix = Path(file.filename).stem[:50] or "report"
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        content = await file.read()
        tmp.write(content)
        zip_path = Path(tmp.name)
    try:
        work_dir, sorted_files = prepare_zip_for_preview(zip_path)
    except Exception as e:
        zip_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(e))
    if not sorted_files:
        zip_path.unlink(missing_ok=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail="В архиве не найдено .doc/.docx")
    zip_path.unlink(missing_ok=True)
    return JSONResponse(_make_preview_response(job_id, sorted_files, suffix + ".pdf", work_dir, user_id))


@app.post("/upload-folder")
async def upload_folder(request: Request, files: List[UploadFile] = File(...)):
    user_id = get_current_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Войдите в аккаунт")
    if not files:
        raise HTTPException(status_code=400, detail="Выберите папку с файлами")
    job_id = str(uuid.uuid4())
    work_dir = Path(tempfile.mkdtemp(prefix="pdf_folder_"))
    written = 0
    for u in files:
        name = u.filename
        if not name or not (name.lower().endswith(".doc") or name.lower().endswith(".docx")):
            continue
        target = work_dir / name.replace("\\", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        content = await u.read()
        target.write_bytes(content)
        written += 1
    if written == 0:
        _cleanup(work_dir)
        raise HTTPException(status_code=422, detail="В папке не найдено .doc/.docx")
    word_files = get_all_word_files(work_dir)
    sorted_files = sort_files_by_pages(word_files)
    return JSONResponse(_make_preview_response(job_id, sorted_files, "merged.pdf", work_dir, user_id))


@app.post("/convert/{job_id}")
async def convert_preview(request: Request, job_id: str):
    user_id = get_current_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Войдите в аккаунт")
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    j = jobs[job_id]
    if j.get("stage") != "preview":
        raise HTTPException(status_code=400, detail="Уже запущено или задание устарело")
    if j.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Нет доступа")
    body = await request.json()
    order = body.get("order")
    if not order or not isinstance(order, list):
        raise HTTPException(status_code=400, detail="Нужен массив order (индексы файлов)")
    work_dir = Path(j["work_dir"])
    sorted_paths = j["sorted_file_paths"]
    order = [int(x) for x in order if isinstance(x, (int, float)) and 0 <= int(x) < len(sorted_paths)]
    if not order:
        raise HTTPException(status_code=400, detail="Выберите хотя бы один файл")
    Thread(target=_run_job_from_preview, args=(job_id, work_dir, order, sorted_paths, j["out_filename"], user_id)).start()
    return JSONResponse({"job_id": job_id, "stage": "processing"})


@app.get("/progress/{job_id}")
def progress(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    j = jobs[job_id]
    return {
        "stage": j["stage"],
        "total": j["total"],
        "current": j["current"],
        "current_file": j.get("current_file", ""),
        "file_names": j.get("file_names", []),
        "total_pages": j.get("total_pages"),
        "error": j.get("error"),
        "done": j.get("done", False),
    }


@app.get("/download/{job_id}")
def download(job_id: str, request: Request, background_tasks: BackgroundTasks):
    user_id = get_current_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Войдите в аккаунт")
    if job_id in jobs:
        j = jobs[job_id]
        if j.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Нет доступа")
        if not j.get("done") or not j.get("pdf_path"):
            raise HTTPException(status_code=400, detail="PDF ещё не готов или произошла ошибка")
        path = Path(j["pdf_path"])
        filename = j.get("filename", "merged.pdf")
    else:
        row = get_job(job_id)
        if not row or row["user_id"] != user_id:
            raise HTTPException(status_code=404, detail="Файл не найден")
        path = Path(row["file_path"])
        filename = row["filename"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл уже удалён")
    return FileResponse(path=str(path), filename=filename, media_type="application/pdf")


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
