# Инструкция по установке и исправлению ошибок

## Установка зависимостей

### 1. Установка всех зависимостей

```bash
pip install -r requirements.txt
```

### 2. Установка pywin32 (для конвертации Word в PDF)

Если используете виртуальное окружение:

```bash
.venv\Scripts\pip install pywin32
.venv\Scripts\python.exe Scripts\pywin32_postinstall.py -install
```

Или если установлен глобально:

```bash
pip install pywin32
python Scripts\pywin32_postinstall.py -install
```

**Важно:** После установки `pywin32` нужно запустить скрипт `pywin32_postinstall.py` для регистрации COM-серверов.

Путь к скрипту обычно:
- В виртуальном окружении: `.venv\Scripts\pywin32_postinstall.py`
- Глобально: `C:\PythonXX\Scripts\pywin32_postinstall.py`

## Исправление ошибки с Updater

### Проблема
Ошибка: `AttributeError: 'Updater' object has no attribute '_Updater__polling_cleanup_cb'`

Это известный баг в версии python-telegram-bot 20.7.

### Решение

```bash
pip uninstall python-telegram-bot
pip install python-telegram-bot --upgrade
```

Или для виртуального окружения:

```bash
.venv\Scripts\pip uninstall python-telegram-bot
.venv\Scripts\pip install python-telegram-bot --upgrade
```

## Проверка установки

Проверьте версию библиотеки:

```bash
python -c "import telegram; print(telegram.__version__)"
```

Проверьте pywin32:

```bash
python -c "import win32com.client; print('pywin32 установлен')"
```

## После установки

Запустите бота:
```bash
python main.py
```

