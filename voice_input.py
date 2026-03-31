"""
Golos 2 — Improved voice input
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

import os, queue, threading, time, json, re, winsound
import numpy as np
import sounddevice as sd
import vosk
import keyboard, pyperclip
from PIL import Image, ImageDraw
import pystray

# ── настройки ─────────────────────────────────────────────
HOTKEY      = "alt+x"
MODEL_PATH  = r"C:\Users\PC\golos2\model"
SAMPLE_RATE = 16000
BLOCK_SIZE  = 2000           # было 4000 — меньше = точнее границы слов
CONFIDENCE_THRESHOLD = 0.45  # порог уверенности (ниже = мусор)
# ──────────────────────────────────────────────────────────

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

    result = t
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
        print("Загрузка модели...")
        vosk.SetLogLevel(-1)
        self.model = vosk.Model(MODEL_PATH)
        self.q = queue.Queue()
        self.recording = False

        # иконка в системном трее
        self.tray = pystray.Icon(
            "golos2",
            ICON_IDLE,
            "Golos 2 — Alt+X",
            menu=pystray.Menu(
                pystray.MenuItem("Выход", self._quit)
            )
        )
        self.tray.run_detached()

        print(f"Готово!  Нажми  {HOTKEY.upper()}  для старта / стопа\n")

    def _quit(self):
        self.recording = False
        self.tray.stop()
        os._exit(0)

    def _audio_cb(self, indata, frames, t, status):
        if status:
            print(f"  [аудио: {status}]")
        # нормализуем громкость перед отправкой в модель
        normalized = normalize_audio(bytes(indata))
        self.q.put(normalized)

    def _record_session(self):
        print(">>> ЗАПИСЬ...  (Alt+X для остановки)")
        rec = vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
        rec.SetWords(True)              # включаем уверенность по словам

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

    def toggle(self):
        if not self.recording:
            while not self.q.empty():
                try: self.q.get_nowait()
                except: break
            self.recording = True
            self.tray.icon = ICON_REC
            self.tray.title = "Golos 2 — REC..."
            winsound.Beep(880, 120)   # высокий бип — старт
            threading.Thread(target=self._record_session, daemon=True).start()
        else:
            self.recording = False
            self.tray.icon = ICON_IDLE
            self.tray.title = "Golos 2 — Alt+X"
            winsound.Beep(440, 200)   # низкий бип — стоп

    def run(self):
        keyboard.add_hotkey(HOTKEY, self.toggle, suppress=True)
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
