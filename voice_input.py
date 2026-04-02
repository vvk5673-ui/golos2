"""
Golos 2.0 — Improved voice input
Alt+X = включить / выключить запись

Голосовые команды (говори во время записи):
  удали          — удалить последнее слово
  удали все      — удалить всё набранное
  точка          — .
  запятая        — ,
  вопрос         — ?
  восклицание    — !
  новая строка   — Enter
  двоеточие      — :
  тире           — —
  абзац          — двойной Enter

Улучшения по сравнению с v3:
  - Уменьшен BLOCK_SIZE (2000) — точнее границы слов
  - SetWords(True) — фильтрация слов по уверенности модели
  - Нормализация громкости аудио — стабильнее распознавание
  - Фильтрация коротких мусорных слов с низкой уверенностью
  - Буфер обмена сохраняется и восстанавливается после вставки
  - «Удали всё» — мгновенное удаление вместо посимвольного
  - Своя копия модели — не зависит от папки golos
"""

import sys, os, queue, threading, time, json, re, winsound

# при запуске из .exe добавляем путь к DLL vosk до его импорта
if getattr(sys, 'frozen', False):
    _vosk_dir = os.path.join(os.path.dirname(sys.executable), "_internal", "vosk")
    os.environ["PATH"] = _vosk_dir + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(_vosk_dir)

import tkinter as tk
import numpy as np
import sounddevice as sd
import vosk
import keyboard, pyperclip
from PIL import Image, ImageDraw
import pystray
from dotenv import load_dotenv

# ── настройки ─────────────────────────────────────────────
# определяем папку приложения — работает и из Python, и из .exe
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
MODEL_PATH  = os.path.join(APP_DIR, "model")
SAMPLE_RATE = 16000
BLOCK_SIZE  = 2000           # было 4000 — меньше = точнее границы слов
CONFIDENCE_THRESHOLD = 0.45  # порог уверенности (ниже = мусор)
DICTIONARY_FILE = os.path.join(APP_DIR, "dictionary.txt")
DEFAULT_HOTKEY = "alt+x"
STARTUP_DIR = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
STARTUP_BAT = os.path.join(STARTUP_DIR, "golos2.bat")

# загружаем .env файл с API-ключами
load_dotenv(os.path.join(APP_DIR, ".env"))
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
# ──────────────────────────────────────────────────────────


def load_config() -> dict:
    """Загружает настройки из config.json. Если файла нет — создаёт с настройками по умолчанию."""
    default = {"hotkey": DEFAULT_HOTKEY}
    if not os.path.exists(CONFIG_FILE):
        save_config(default)
        return default
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_config(config: dict):
    """Сохраняет настройки в config.json."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)


def is_autostart_enabled() -> bool:
    """Проверяет, включён ли автозапуск (есть ли .bat в автозагрузке)."""
    return os.path.exists(STARTUP_BAT)


def set_autostart(enabled: bool):
    """Включает или выключает автозапуск через папку автозагрузки Windows."""
    if enabled:
        script_path = os.path.join(APP_DIR, "voice_input.py")
        with open(STARTUP_BAT, "w", encoding="ascii") as f:
            f.write(f'@echo off\ncd /d "{APP_DIR}"\npython "{script_path}"\n')
        print("Автозапуск включён")
    else:
        if os.path.exists(STARTUP_BAT):
            os.remove(STARTUP_BAT)
        print("Автозапуск выключен")


def load_dictionary(filepath: str) -> dict:
    """Загружает словарь автозамены из файла dictionary.txt"""
    result = {}
    if not os.path.exists(filepath):
        return result
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                wrong, correct = line.split("=", 1)
                result[wrong.strip().lower()] = correct.strip()
    return result


AUTOCORRECT = load_dictionary(DICTIONARY_FILE)

PUNCT = {
    # знаки препинания
    "точка":           ".",
    "запятая":         ",",
    "вопрос":          "?",
    "вопросительный":  "?",
    "восклицание":     "!",
    "восклицательный": "!",
    "двоеточие":       ":",
    "точка с запятой": ";",
    "тире":            " — ",
    "дефис":           "-",
    "многоточие":      "...",
    "троеточие":       "...",
    # скобки и кавычки
    "открыть скобку":  "(",
    "закрыть скобку":  ")",
    "скобка":          "(",
    "конец скобки":    ")",
    "кавычки":         "\"",
    "открыть кавычки": "«",
    "закрыть кавычки": "»",
    "ёлочки":          "«",
    "конец ёлочек":    "»",
    # спецсимволы
    "собака":          "@",
    "собачка":         "@",
    "решётка":         "#",
    "хештег":          "#",
    "номер":           "№",
    "процент":         "%",
    "амперсанд":       "&",
    "звёздочка":       "*",
    "плюс":            "+",
    "равно":           "=",
    "слэш":            "/",
    "обратный слэш":   "\\",
    "доллар":          "$",
    "евро":            "€",
    "рубль":           "₽",
    # форматирование
    "новая строка":    "\n",
    "с новой строки":  "\n",
    "абзац":           "\n\n",
    "табуляция":       "\t",
    "таб":             "\t",
    "пробел":          " ",
}

DELETE_CMDS     = {"удали", "удалить", "удали слово", "стереть", "стери", "удали это"}
DELETE_ALL_CMDS = {"удали все", "удалить все", "стереть все", "удали всё", "удалить всё", "стереть всё"}


def make_tray_icon(color: str, dot: bool = False) -> Image.Image:
    """Рисуем иконку микрофона для трея."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill=color)
    # белый символ микрофона
    d.rectangle([24, 16, 40, 40], fill="white", outline="white")
    d.ellipse([20, 32, 44, 52], fill="white")
    d.rectangle([30, 50, 34, 58], fill="white")
    d.line([22, 58, 42, 58], fill="white", width=3)
    if dot:
        # красная точка записи в углу
        d.ellipse([44, 4, 60, 20], fill="#ff3b30")
    return img


ICON_IDLE = make_tray_icon("#555555")          # серый — ожидание
ICON_REC  = make_tray_icon("#34c759", dot=True) # зелёный + красная точка — запись


def normalize_audio(data: bytes) -> bytes:
    """Нормализация громкости аудио — выравнивает тихую и громкую речь.
    Не трогает тихие блоки (тишина/паузы), чтобы не усиливать шум."""
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    max_val = np.max(np.abs(audio))
    # если блок слишком тихий (< 500) — это тишина, не трогаем
    if max_val > 500:
        target = 20000.0
        ratio = target / max_val
        # ограничиваем усиление максимум в 3 раза, чтобы не раздувать шум
        ratio = min(ratio, 3.0)
        if ratio > 1.1 or ratio < 0.9:
            audio = audio * ratio
            audio = np.clip(audio, -32768, 32767)
    return audio.astype(np.int16).tobytes()


def filter_by_confidence(result: dict) -> str:
    """Фильтрация слов по уровню уверенности модели.
    Убирает мусорные слова, в которых модель не уверена."""
    words = result.get("result", [])
    if not words:
        # если нет слов с уверенностью — вернуть как есть
        return result.get("text", "").strip()

    filtered = []
    for w in words:
        word = w.get("word", "")
        conf = w.get("conf", 0)
        # короткие слова (1-2 буквы) с низкой уверенностью — почти всегда мусор
        if len(word) <= 2 and conf < 0.6:
            continue
        # обычные слова фильтруем по основному порогу
        if conf >= CONFIDENCE_THRESHOLD:
            filtered.append(word)

    return " ".join(filtered)


def apply_autocorrect(text: str) -> str:
    """Автозамена слов из dictionary.txt"""
    if not AUTOCORRECT:
        return text
    result = text
    for wrong, correct in AUTOCORRECT.items():
        result = re.sub(rf"(?i)\b{re.escape(wrong)}\b", lambda m: correct, result)
    return result


def type_text(text: str) -> int:
    """Вставляет текст через буфер обмена, сохраняя его предыдущее содержимое."""
    if not text:
        return 0
    # сохраняем то, что было в буфере
    try:
        old_clipboard = pyperclip.paste()
    except Exception:
        old_clipboard = ""
    pyperclip.copy(text)
    keyboard.press_and_release("ctrl+v")
    time.sleep(0.08)
    # восстанавливаем буфер обмена
    try:
        pyperclip.copy(old_clipboard)
    except Exception:
        pass
    return len(text)


def process_text(text: str, chars_typed: list) -> bool:
    t = text.strip()
    low = t.lower()

    if low in DELETE_ALL_CMDS:
        if chars_typed[0] > 0:
            # выделяем назад все набранные символы и удаляем одним нажатием
            keyboard.press_and_release(f"shift+home")
            time.sleep(0.05)
            keyboard.press_and_release("backspace")
            chars_typed[0] = 0
            print("  [удалено всё]")
        return False

    if low in DELETE_CMDS:
        keyboard.press_and_release("ctrl+backspace")
        # не пытаемся угадать длину слова — просто уменьшаем на примерную величину
        chars_typed[0] = max(0, chars_typed[0] - 10)
        print("  [удалено слово]")
        return False

    # автозамена из словаря
    result = apply_autocorrect(t)

    # голосовые команды пунктуации
    for cmd, sym in PUNCT.items():
        result = re.sub(rf"(?i)\b{re.escape(cmd)}\b", lambda m: sym, result)

    result = re.sub(r" ([.,!?:;])", r"\1", result)
    result = re.sub(r"([.,!?:;])([^\s\n])", r"\1 \2", result)

    if result and result[0].islower():
        result = result[0].upper() + result[1:]

    if result.strip():
        typed = type_text(result + " ")
        chars_typed[0] += typed
        return True
    return False


class VoiceInput:
    def __init__(self):
        self.q = queue.Queue()
        self.recording = False

        # загружаем горячую клавишу и движок из конфига
        self.config = load_config()
        self.hotkey = self.config.get("hotkey", DEFAULT_HOTKEY)
        self.engine = self.config.get("engine", "vosk")  # "vosk" или "deepgram"

        # загружаем Vosk-модель (нужна всегда как запасной вариант)
        print("Загрузка модели Vosk...")
        vosk.SetLogLevel(-1)
        self.model = vosk.Model(MODEL_PATH)

        # проверяем Deepgram API-ключ
        if DEEPGRAM_API_KEY:
            print(f"Deepgram API-ключ: найден")
        else:
            if self.engine == "deepgram":
                print("ВНИМАНИЕ: Deepgram API-ключ не найден в .env! Переключаюсь на Vosk.")
                self.engine = "vosk"

        # иконка в системном трее
        engine_label = "Deepgram" if self.engine == "deepgram" else "Vosk"
        self.tray = pystray.Icon(
            "golos2",
            ICON_IDLE,
            f"Golos 2.0 [{engine_label}] — {self.hotkey.upper()}",
            menu=self._build_menu()
        )
        self.tray.run_detached()

        print(f"Движок: {engine_label}")
        print(f"Готово!  Нажми  {self.hotkey.upper()}  для старта / стопа\n")

    def _build_menu(self):
        """Собирает меню трея."""
        engine_label = "Deepgram" if self.engine == "deepgram" else "Vosk"
        other_engine = "Deepgram" if self.engine == "vosk" else "Vosk"
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: f"Движок: {engine_label}  (переключить на {other_engine})",
                self._toggle_engine
            ),
            pystray.MenuItem(
                lambda item: f"Горячая клавиша: {self.hotkey.upper()}",
                self._change_hotkey
            ),
            pystray.MenuItem(
                lambda item: f"Автозапуск: {'вкл' if is_autostart_enabled() else 'выкл'}",
                self._toggle_autostart
            ),
            pystray.MenuItem("Выход", self._quit)
        )

    def _toggle_engine(self):
        """Переключает движок распознавания между Vosk и Deepgram."""
        if self.engine == "vosk":
            if not DEEPGRAM_API_KEY:
                print("Deepgram API-ключ не найден в .env! Добавьте DEEPGRAM_API_KEY=ваш_ключ")
                return
            self.engine = "deepgram"
        else:
            self.engine = "vosk"

        # сохраняем выбор
        self.config["engine"] = self.engine
        save_config(self.config)

        engine_label = "Deepgram" if self.engine == "deepgram" else "Vosk"
        self.tray.title = f"Golos 2.0 [{engine_label}] — {self.hotkey.upper()}"
        self.tray.menu = self._build_menu()
        self.tray.update_menu()
        print(f"Движок переключён на: {engine_label}")

    def _toggle_autostart(self):
        """Включает/выключает автозапуск."""
        enabled = is_autostart_enabled()
        set_autostart(not enabled)
        self.tray.menu = self._build_menu()
        self.tray.update_menu()

    def _quit(self):
        self.recording = False
        self.tray.stop()
        os._exit(0)

    def _change_hotkey(self):
        """Открывает маленькое окошко для смены горячей клавиши."""
        threading.Thread(target=self._hotkey_window, daemon=True).start()

    def _hotkey_window(self):
        """Окно захвата новой горячей клавиши."""
        win = tk.Tk()
        win.title("Golos 2.0 — Смена клавиши")
        win.geometry("350x150")
        win.resizable(False, False)
        win.attributes("-topmost", True)

        # центрируем окно на экране
        win.update_idletasks()
        x = (win.winfo_screenwidth() // 2) - 175
        y = (win.winfo_screenheight() // 2) - 75
        win.geometry(f"+{x}+{y}")

        label = tk.Label(win, text=f"Сейчас: {self.hotkey.upper()}", font=("Arial", 14))
        label.pack(pady=(15, 5))

        hint = tk.Label(win, text="Нажмите новую комбинацию клавиш...", font=("Arial", 11), fg="gray")
        hint.pack(pady=5)

        result_label = tk.Label(win, text="", font=("Arial", 13, "bold"), fg="green")
        result_label.pack(pady=5)

        def on_key(event):
            # собираем комбинацию из модификаторов + клавиша
            parts = []
            if event.state & 0x20000 or event.keysym in ("Alt_L", "Alt_R"):
                parts.append("alt")
            if event.state & 0x4:
                parts.append("ctrl")
            if event.state & 0x1:
                parts.append("shift")

            # не сохраняем если нажат только модификатор
            key = event.keysym.lower()
            if key in ("alt_l", "alt_r", "control_l", "control_r", "shift_l", "shift_r"):
                return

            parts.append(key)
            new_hotkey = "+".join(parts)

            # сохраняем новую клавишу
            self._apply_new_hotkey(new_hotkey)

            result_label.config(text=f"Установлено: {new_hotkey.upper()}")
            hint.config(text="Окно закроется через 1 сек...")
            win.after(1000, win.destroy)

        win.bind("<Key>", on_key)
        win.mainloop()

    def _apply_new_hotkey(self, new_hotkey: str):
        """Применяет новую горячую клавишу: перерегистрирует и сохраняет в конфиг."""
        # убираем старую горячую клавишу
        try:
            keyboard.remove_hotkey(self.hotkey)
        except Exception:
            pass

        # ставим новую
        self.hotkey = new_hotkey
        keyboard.add_hotkey(self.hotkey, self.toggle, suppress=True)

        # обновляем конфиг
        self.config["hotkey"] = new_hotkey
        save_config(self.config)

        # обновляем трей: подсказку и меню
        self.tray.title = f"Golos 2.0 — {new_hotkey.upper()}"
        self.tray.menu = self._build_menu()
        self.tray.update_menu()

        print(f"Горячая клавиша изменена на: {new_hotkey.upper()}")

    def _audio_cb(self, indata, frames, t, status):
        if status:
            print(f"  [аудио: {status}]")
        if self.engine == "deepgram":
            # Deepgram сам нормализует аудио — отправляем как есть
            self.q.put(bytes(indata))
        else:
            # Vosk нужна нормализация
            self.q.put(normalize_audio(bytes(indata)))

    def _record_session(self):
        """Запускает сессию записи через выбранный движок."""
        if self.engine == "deepgram" and DEEPGRAM_API_KEY:
            self._record_session_deepgram()
        else:
            self._record_session_vosk()

    def _record_session_vosk(self):
        """Сессия записи через Vosk (оффлайн)."""
        print(">>> ЗАПИСЬ [Vosk]...  (Alt+X для остановки)")
        rec = vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
        rec.SetWords(True)

        last_partial = ""
        chars_typed = [0]

        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                               dtype="int16", channels=1,
                               callback=self._audio_cb):
            while self.recording:
                try:
                    data = self.q.get(timeout=0.2)
                except queue.Empty:
                    continue

                if rec.AcceptWaveform(data):
                    res = json.loads(rec.Result())
                    text = filter_by_confidence(res)

                    if text:
                        print(f"\r  → {text}           ")
                        process_text(text, chars_typed)
                    last_partial = ""
                else:
                    partial = json.loads(rec.PartialResult()).get("partial", "")
                    if partial and partial != last_partial:
                        print(f"\r  … {partial}   ", end="", flush=True)
                        last_partial = partial

            res = json.loads(rec.FinalResult())
            text = filter_by_confidence(res)

            if text:
                print(f"\r  → {text}           ")
                process_text(text, chars_typed)

        print("\n<<< ОСТАНОВЛЕНО\n")

    def _record_session_deepgram(self):
        """Сессия записи через Deepgram (облако, нужен интернет)."""
        import websockets.sync.client as ws_client

        print(">>> ЗАПИСЬ [Deepgram Nova-3]...  (Alt+X для остановки)")
        chars_typed = [0]
        last_partial = ""

        # слова-подсказки для Deepgram (лучше распознаёт)
        keyterms = [
            # имена и люди
            "Виктор", "Коротков", "Татьяна", "Таня",
            # проекты Виктора
            "Голос", "Golos", "MyCash", "МойКэш", "MyLending",
            "лендинг", "портфолио",
            # IT-термины
            "код", "промпт", "промптинг", "нейросеть", "нейросети",
            "чатбот", "вайбкодинг",
            "API", "Python", "JavaScript", "HTML", "CSS",
            "GitHub", "Vercel", "Claude", "Deepgram", "Vosk",
            "вебсайт", "фронтенд", "бэкенд",
            "фреймворк", "библиотека", "репозиторий", "коммит",
            "деплой", "хостинг", "домен", "сервер",
            "WebSocket", "SDK", "JSON",
            # курс и обучение
            "Нейроуниверситет", "CRAFT",
            # программирование
            "файл", "папка", "терминал", "консоль",
            "функция", "переменная", "массив", "объект",
            "интерфейс", "компонент", "модуль", "пакет",
            "баг", "фикс", "тест", "релиз", "версия",
            "база данных", "запрос", "ответ", "токен",
            "авторизация", "аутентификация", "пароль", "логин",
            # инструменты
            "VS Code", "Claude Code", "Obsidian", "Telegram",
            "Cursor", "PyInstaller",
            # оборудование и сеть
            "роутер", "маршрутизатор", "модем",
            "TP-Link", "Archer", "Wi-Fi", "LTE",
            "гигабит", "мегабит",
            "VPN", "прокси", "SOCKS",
            # умные устройства
            "Smart Life", "термостат", "камера",
            # бизнес и финансы
            "бюджет", "выручка", "расход", "прибыль",
            "клиент", "проект", "задача", "дедлайн",
            "бизнес", "предприниматель",
            # нейро-фото
            "Krea", "LoRA", "Flux",
            # география
            "Павловка", "Саратовская",
        ]
        import urllib.parse
        keyterms_params = "&".join(
            f"keyterm={urllib.parse.quote(k)}" for k in keyterms
        )

        url = (
            "wss://api.deepgram.com/v1/listen?"
            "model=nova-3&language=ru&encoding=linear16"
            f"&sample_rate={SAMPLE_RATE}&channels=1"
            "&interim_results=true&punctuate=true"
            "&numerals=true&endpointing=300&utterance_end_ms=1000"
            f"&{keyterms_params}"
        )
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}

        try:
            ws = ws_client.connect(url, additional_headers=headers, open_timeout=10)
            print("  Подключение к Deepgram установлено!")
        except Exception as e:
            print(f"  Не удалось подключиться к Deepgram: {e}")
            print("  Проверьте интернет и API-ключ.")
            return

        # поток для отправки аудио — чтобы не было задержек
        send_active = True
        def audio_sender():
            while send_active and self.recording:
                try:
                    data = self.q.get(timeout=0.05)
                    ws.send(data)
                except queue.Empty:
                    pass
                except Exception:
                    break

        sender_thread = threading.Thread(target=audio_sender, daemon=True)

        try:
            with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                                   dtype="int16", channels=1,
                                   callback=self._audio_cb):
                sender_thread.start()

                while self.recording:
                    try:
                        msg = ws.recv(timeout=0.3)
                        data = json.loads(msg)

                        if data.get("type") != "Results":
                            continue

                        ch = data.get("channel", {})
                        alternatives = ch.get("alternatives", [{}])
                        transcript = alternatives[0].get("transcript", "").strip()

                        if not transcript:
                            continue

                        is_final = data.get("is_final", False)

                        if is_final:
                            print(f"\r  → {transcript}           ")
                            process_text(transcript, chars_typed)
                            last_partial = ""
                        else:
                            if transcript != last_partial:
                                print(f"\r  … {transcript}   ", end="", flush=True)
                                last_partial = transcript

                    except TimeoutError:
                        continue
                    except Exception as e:
                        print(f"  [Deepgram ошибка: {e}]")
                        break
        except Exception as e:
            print(f"  [Ошибка записи: {e}]")
        finally:
            send_active = False
            try:
                ws.close()
            except Exception:
                pass
            print("\n<<< ОСТАНОВЛЕНО\n")

    def toggle(self):
        if not self.recording:
            while not self.q.empty():
                try: self.q.get_nowait()
                except: break
            self.recording = True
            self.tray.icon = ICON_REC
            self.tray.title = "Golos 2.0 — REC..."
            winsound.Beep(880, 120)   # высокий бип — старт
            threading.Thread(target=self._record_session, daemon=True).start()
        else:
            self.recording = False
            self.tray.icon = ICON_IDLE
            self.tray.title = "Golos 2.0 — Alt+X"
            winsound.Beep(440, 200)   # низкий бип — стоп

    def run(self):
        keyboard.add_hotkey(self.hotkey, self.toggle, suppress=True)
        keyboard.wait()


if __name__ == "__main__":
    try:
        VoiceInput().run()
    except KeyboardInterrupt:
        print("\nВыход.")
    except Exception as e:
        print(f"Ошибка: {e}")
        import traceback; traceback.print_exc()
        input("Нажми Enter для выхода...")
