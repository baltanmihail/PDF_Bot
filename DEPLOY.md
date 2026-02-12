# Запуск на Railway с нуля

Пошаговая инструкция: от репозитория до работающего сайта с аккаунтами и хранением PDF.

---

```bash
git add .
git commit -m "описание изменений"
git push
```

## Чеклист после деплоя (обязательно)

Если контейнер уже запустился, но нужны аккаунты и сохранение файлов:

1. **Volume**  
   В сервисе: **Settings** → **Volumes** → **Add Volume**.  
   Mount Path: **`/data`**.

2. **Переменные (Variables)**  
   В сервисе: **Variables** → **Add Variable** (или **Raw Editor**). Добавьте:
   - `SECRET_KEY` = длинная случайная строка (например: `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
   - `DATA_DIR` = `/data/outputs`
   - `DB_PATH` = `/data/auth.db`

3. **Перезапуск**  
   После добавления тома и переменных нажмите **Redeploy** (или новый деплой через **Deploy**).

---

## 1. Подготовка репозитория

- Код уже в Git (GitHub / GitLab).
- В корне есть: `web_app.py`, `core.py`, `auth_db.py`, `Dockerfile`, `requirements.txt`, `Procfile`.

---

## 2. Создание проекта в Railway

1. Зайдите на [railway.app](https://railway.app), войдите через GitHub.
2. **New Project** → **Deploy from GitHub repo**.
3. Выберите репозиторий с PDF_Bot.
4. Railway определит сборку (по `Dockerfile` или по `Procfile`). Если есть **Dockerfile** — лучше использовать его (в нём уже установлен LibreOffice для конвертации Word → PDF на Linux).

---

## 3. Volume для хранения PDF и БД

Без тома все файлы и SQLite пропадут при перезапуске.

1. В проекте откройте ваш **сервис**.
2. Вкладка **Variables** или **Settings** → **Volumes** (или **Add Volume**).
3. Создайте том, например имя `data`, путь монтирования: **`/data`**.
4. В **Variables** добавьте переменные:
   - `DATA_DIR=/data/outputs` — каталог для готовых PDF (внутри тома).
   - `DB_PATH=/data/auth.db` — путь к SQLite (на томе).
   - `SECRET_KEY=<случайная строка>` — для подписи сессий. Сгенерировать можно так:
     ```bash
     python -c "import secrets; print(secrets.token_urlsafe(32))"
     ```

Переменная `PORT` на Railway задаётся автоматически, менять не нужно.

---

## 4. Сборка и запуск

- Приложение запускается как `python web_app.py` и само читает порт из переменной **PORT** (Railway задаёт её автоматически).
- Если используется **Dockerfile**: Railway соберёт образ (Python + LibreOffice) и запустит приложение.
- Если без Dockerfile (только Procfile): в сервисе должен быть выбран **Stack** с поддеркой Python; на Railway при наличии `Procfile` часто используют Nixpacks. Тогда **LibreOffice не будет установлен** — конвертация Word не заработает. Имеет смысл оставить **Dockerfile** как основной способ деплоя.

После деплоя Railway покажет URL вида `https://your-app.up.railway.app`.

---

## 5. Проверка

1. Откройте URL в браузере.
2. Должна открыться страница входа (логин / регистрация).
3. Зарегистрируйтесь: логин = часть email до `@`, пароль — любой (≥ 4 символов).
4. После входа: форма загрузки ZIP или папки, прогресс, блок «Мои файлы».
5. Загрузите тестовый ZIP с .doc/.docx → дождитесь готовности → скачайте PDF. Файл сохранится на томе и появится в «Мои файлы».

---

## 6. Переменные окружения (сводка)

| Переменная    | Обязательно | Описание |
|---------------|-------------|----------|
| `SECRET_KEY`  | Да          | Секрет для подписи сессий (длинная случайная строка). |
| `DATA_DIR`    | Да (на Railway) | Каталог для PDF, например `/data/outputs`. Должен лежать на Volume. |
| `DB_PATH`     | Нет         | Путь к SQLite. По умолчанию `data/auth.db`. На Railway лучше `/data/auth.db`. |
| `PORT`        | Нет         | Задаёт Railway автоматически. |

---

## 7. Где хранятся данные

- **Пользователи и задания:** SQLite-файл по пути `DB_PATH` (на томе — `/data/auth.db`).
- **Готовые PDF:** каталог `DATA_DIR`; внутри подпапки по `user_id`, в них — файлы PDF. Всё это на Volume, чтобы не терялось при рестартах.

Google Drive / OAuth подключать не обязательно: для «моих файлов» и скачивания достаточно тома на Railway. Если позже понадобится выгрузка в Google Drive, можно добавить отдельный модуль с Google API и переменными `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` и т.д.

---

## 8. Локальный запуск (без Docker)

Для проверки с аккаунтами и хранением локально:

```bash
pip install -r requirements.txt
set SECRET_KEY=local-secret
set DATA_DIR=./data/outputs
set DB_PATH=./data/auth.db
uvicorn web_app:app --host 127.0.0.1 --port 8000
```

Откройте http://127.0.0.1:8000. На Windows конвертация пойдёт через Word (COM), не через LibreOffice.
