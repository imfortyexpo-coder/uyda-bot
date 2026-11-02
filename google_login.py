"""
Вход в Google для бота «UYDA Импорт» — делается ОДИН раз.

Бот будет читать Google Таблицу с вашими правами «Читатель» (только чтение, менять таблицу он не может).
Запуск: в папке бота откройте cmd и выполните
    py -3.12 google_login.py

Что делает:
  1) открывает в браузере вход в Google → вы нажимаете «Разрешить»;
  2) сохраняет ключ входа в google_token.json (рядом с ботом; никому не отправляйте, в GitHub не попадает);
  3) просит ссылку на таблицу, проверяет, что бот её читает (сколько партий, жёлтые ячейки, примечания);
  4) по вашему «да» переключает бота с Excel-файла на Google Таблицу (строки в env).
"""

import glob
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import config as cfg  # noqa: E402

LINE = "=" * 70


def say(text: str = "") -> None:
    print(text, flush=True)


def ensure_oauthlib():
    try:
        import google_auth_oauthlib  # noqa: F401
    except ImportError:
        say("Устанавливаю недостающий модуль google-auth-oauthlib (один раз)…")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "google-auth-oauthlib>=1.2"])
        if r.returncode != 0:
            raise SystemExit("Не удалось установить. Выполните вручную:  py -3.12 -m pip install google-auth-oauthlib")


def find_client_file() -> str:
    """Файл OAuth-клиента из Google Cloud: google_oauth_client.json или скачанный client_secret_….json."""
    main = cfg.in_base(cfg.GOOGLE_OAUTH_CLIENT_FILE)
    if os.path.exists(main):
        return main
    found = sorted(glob.glob(os.path.join(HERE, "client_secret*.json")), key=os.path.getmtime, reverse=True)
    if found:
        shutil.copyfile(found[0], main)
        say(f"Нашёл файл {os.path.basename(found[0])} — сохранил как {os.path.basename(main)}.")
        return main
    say(LINE)
    say("❗ Нет файла OAuth-клиента Google.")
    say("   Google Cloud → «APIs и сервисы» → «Учётные данные» → «Создать» → «Идентификатор клиента OAuth»")
    say("   → тип «Приложение для ПК» → «Создать» → «Скачать JSON».")
    say(f"   Положите скачанный файл (client_secret_….json) в папку бота: {HERE}")
    say("   и запустите этот скрипт ещё раз.")
    say(LINE)
    raise SystemExit(1)


def login(client_file: str):
    from google_auth_oauthlib.flow import InstalledAppFlow
    say("\nСейчас откроется браузер. Войдите в Google той почтой, у которой есть доступ к таблице,")
    say("и нажмите «Разрешить». Если Google напишет «Приложение не проверено» —")
    say("нажмите «Дополнительно» → «Перейти на страницу…» (это ваше собственное приложение).\n")
    flow = InstalledAppFlow.from_client_secrets_file(client_file, cfg.GOOGLE_SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                  success_message="Готово! Вход выполнен — закройте эту вкладку и вернитесь в окно cmd.")
    token = cfg.in_base(cfg.GOOGLE_TOKEN_FILE)
    with open(token, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    if not creds.refresh_token:
        say("⚠️ Google не выдал постоянный ключ. Удалите доступ приложения в myaccount.google.com → «Безопасность»"
            " → «Сторонние приложения» и запустите скрипт ещё раз.")
    say(f"✅ Вход выполнен, ключ сохранён: {os.path.basename(token)}")
    return creds


def parse_link(text: str) -> tuple[str, str]:
    """Ссылка на таблицу -> (ID таблицы, номер листа gid или '')."""
    text = (text or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", text)
    sid = m.group(1) if m else (text if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text) else "")
    g = re.search(r"[#&?]gid=(\d+)", text)
    return sid, (g.group(1) if g else "")


def ask_sheet() -> tuple[str, str]:
    if cfg.SPREADSHEET_ID:
        say(f"\nВ env уже указана таблица: {cfg.SPREADSHEET_ID}" + (f" (лист {cfg.WORKSHEET_GID})" if cfg.WORKSHEET_GID else ""))
        ans = input("Оставить её? Enter — да, или вставьте другую ссылку: ").strip()
        if not ans:
            return cfg.SPREADSHEET_ID, cfg.WORKSHEET_GID
        sid, gid = parse_link(ans)
    else:
        sid = gid = ""
        for _ in range(3):
            ans = input("\nВставьте ссылку на таблицу импорта (из адресной строки браузера) и нажмите Enter:\n> ")
            sid, gid = parse_link(ans)
            if sid:
                break
            say("Это не похоже на ссылку Google Таблицы (нужен вид https://docs.google.com/spreadsheets/d/…).")
        else:
            raise SystemExit("Ссылка не введена.")
    return sid, gid


def check(creds, sid: str, gid: str) -> str:
    """Открыть таблицу, показать листы, прочитать как бот. -> gid листа, который читаем."""
    import gspread
    import sheet as sh
    gc = gspread.authorize(creds)
    book = gc.open_by_key(sid)
    sheets = book.worksheets()
    say(f"\n📄 Таблица: «{book.title}». Листы:")
    for ws in sheets:
        say(f"   • {ws.title}  (gid={ws.id}, строк: {ws.row_count})")
    if not gid:
        gid = str(sheets[0].id)
        say(f"Ссылка без номера листа — беру первый лист: «{sheets[0].title}».")
    cfg.SPREADSHEET_ID, cfg.WORKSHEET_GID = sid, gid
    say("\nЧитаю таблицу так же, как бот (может занять до минуты)…")
    reader = sh.SheetReader()
    reader._gc, reader.mode = gc, "oauth"
    records = reader._fetch()
    yellow = sum(len(r.get("unconfirmed", [])) for r in records)
    notes = sum(1 for r in records if r.get("cargo_note"))
    say(f"✅ Прочитано строк-партий: {len(records)}; жёлтых (неподтверждённых) сумм: {yellow}; примечаний: {notes}.")
    if records:
        say(f"   Последняя партия в таблице: {records[-1]['title']}")
    return gid


def update_env(sid: str, gid: str) -> None:
    path = cfg.ENV_FILE or os.path.join(HERE, "env")
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    want = {"GOOGLE_SPREADSHEET_ID": sid, "GOOGLE_WORKSHEET_GID": gid}
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in want:
            out.append(f"{key}={want[key]}")
            seen.add(key)
        elif key == "EXCEL_FILE" and not line.lstrip().startswith("#"):
            out.append("# " + line + "    # выключено: бот читает Google Таблицу (вернуть — убрать # в начале)")
        else:
            out.append(line)
    for key, val in want.items():
        if key not in seen:
            out.append(f"{key}={val}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    say(f"✅ Записано в {os.path.basename(path)}: таблица и лист; Excel-файл выключен.")


def main() -> None:
    say(LINE)
    say(" UYDA Импорт — вход в Google (один раз). Бот получит только ЧТЕНИЕ таблиц.")
    say(LINE)
    ensure_oauthlib()
    client = find_client_file()
    creds = login(client)
    sid, gid = ask_sheet()
    try:
        gid = check(creds, sid, gid)
    except Exception as e:
        say(f"\n❌ Не получилось прочитать таблицу: {type(e).__name__}: {str(e)[:300]}")
        say("   Проверьте, что эта таблица открывается у вас в браузере той же почтой. Пришлите снимок окна Claude.")
        raise SystemExit(1)
    ans = input("\nПереключить бота на эту Google Таблицу? (да / нет): ").strip().lower()
    if ans in ("да", "д", "yes", "y", "ha", "xa"):
        update_env(sid, gid)
        say("\nГотово! Перезапустите бота: в его окне Ctrl+C, затем start.bat.")
        say("В /status будет: «Google Таблица (вход через ваш Google-аккаунт, только чтение)».")
    else:
        say("Хорошо, env не менял. Бот пока читает Excel-файл.")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            input("\nНажмите Enter, чтобы закрыть…")
        except EOFError:
            pass
