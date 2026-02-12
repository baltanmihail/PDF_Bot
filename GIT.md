# Git: репозиторий и обновление кода

## 1. Создать репозиторий на GitHub

1. Зайдите на [github.com](https://github.com), войдите в аккаунт.
2. Нажмите **+** (или **New repository**).
3. Укажите имя, например: `PDF_Bot`.
4. Можно оставить **Private** или выбрать **Public**.
5. **Не** ставьте галочку "Add a README" — репозиторий создайте пустым.
6. Нажмите **Create repository**.

На странице репозитория GitHub покажет команды. Ниже — те же шаги по шагам.

---

## 2. Первая загрузка кода (если Git ещё не инициализирован)

Откройте терминал в папке проекта (например, в Cursor: **Terminal → New Terminal** или `Ctrl+`` `).

```powershell
cd "c:\Users\click\OneDrive\Рабочий стол\МГТУ\Python Projects\PDF_Bot"
```

Инициализация и первый коммит:

```powershell
git init
git add .
git commit -m "Первый коммит: бот, веб-приложение, аккаунты, превью"
```

Подключите удалённый репозиторий (подставьте **свой** логин и имя репозитория):

```powershell
git remote add origin https://github.com/ВАШ_ЛОГИН/PDF_Bot.git
```

Отправьте код на GitHub:

```powershell
git branch -M main
git push -u origin main
```

Если попросит авторизацию — войдите через браузер или по токену (GitHub → Settings → Developer settings → Personal access tokens).

---

## 3. Обновлять код (когда что-то изменили)

Каждый раз после изменений в проекте:

```powershell
cd "c:\Users\click\OneDrive\Рабочий стол\МГТУ\Python Projects\PDF_Bot"

git add .
git status
git commit -m "Кратко: что сделали, например: добавлена проверка порядка файлов"
git push
```

- `git add .` — подготовить все изменения.
- `git status` — посмотреть, что попало в коммит (по желанию).
- `git commit -m "..."` — зафиксировать с сообщением.
- `git push` — отправить на GitHub.

---

## 4. Что не попадает в репозиторий

В проекте должен быть файл **`.gitignore`**, чтобы не загружать лишнее:

- `config.py` — токен бота (секрет).
- `data/` — локальная БД и PDF (если есть).
- `__pycache__/`, `.venv/` — кэш и виртуальное окружение.

Если `.gitignore` нет или в нём чего-то не хватает — создайте или допишите, например:

```
config.py
data/
.venv/
__pycache__/
*.pyc
.env
```

Проверить, что в коммит не попал секрет:

```powershell
git status
```

В списке не должно быть `config.py`.

---

## 5. Подключить репозиторий к Railway

В [Railway](https://railway.app): **New Project → Deploy from GitHub repo** → выберите репозиторий `PDF_Bot`. Деплой будет идти из ветки `main`: при каждом `git push` Railway может автоматически пересобирать и обновлять сайт (если включен автодеплой).

---

## Краткая шпаргалка

| Действие              | Команды |
|-----------------------|--------|
| Первый раз загрузить  | `git init` → `git add .` → `git commit -m "..."` → `git remote add origin URL` → `git push -u origin main` |
| Обновить код на GitHub| `git add .` → `git commit -m "описание"` → `git push` |
| Посмотреть статус    | `git status` |
