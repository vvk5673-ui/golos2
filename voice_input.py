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
  - Подавление фонового шума (noisereduce)
  - Фильтр тишины (VAD) — не отправляет паузы в модель
  - Нормализация громкости аудио
  - Фильтрация слов по уверенности модели (SetWords)
  - Автозамена типичных ошибок распознавания
  - Склейка коротких фраз в единый текст
  - Буфер обмена сохраняется и восстанавливается
  - 40 голосовых команд (пунктуация, скобки, спецсимволы)
  - Своя копия модели — не зависит от папки golos
"""

import os, queue, threading, time, json, re, winsound
import numpy as np
import noisereduce as nr
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
SILENCE_THRESHOLD = 300      # порог тишины — блоки тише этого не отправляем в модель
# ──────────────────────────────────────────────────────────

# автозамена типичных ошибок распознавания
AUTOCORRECT = {
    "кот": "код",
    "коты": "коды",
    "промыт": "промпт",
    "промыть": "промпт",
    "пром": "промпт",
    "контекст а": "контекста",
    "нейро сеть": "нейросеть",
    "нейро сети": "нейросети",
    "чат бот": "чатбот",
    "веб сайт": "вебсайт",
    "он лайн": "онлайн",
    "оф лайн": "офлайн",
    "может быт": "может быть",
    "потому шта": "потому что",
    "тока": "только",
    "щас": "сейчас",
    "чё": "что",
    "те": "тебе",
    "ваще": "вообще",
}

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


def is_silence(data: bytes) -> bool:
    """Проверяет, является ли блок тишиной (VAD — Voice Activity Detection)."""
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    rms = np.sqrt(np.mean(audio ** 2))
    return rms < SILENCE_THRESHOLD


def reduce_noise(data: bytes) -> bytes:
    """Подавление фонового шума — убирает вентилятор, клавиатуру и т.д."""
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    # noisereduce убирает стационарный шум (гул, вентилятор)
    cleaned = nr.reduce_noise(y=audio, sr=SAMPLE_RATE, stationary=True, prop_decrease=0.6)
    cleaned = np.clip(cleaned, -32768, 32767)
    return cleaned.astype(np.int16).tobytes()


def normalize_audio(data: bytes) -> bytes:
    """Нормализация громкости аудио — выравнивает тихую и громкую речь."""
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    max_val = np.max(np.abs(audio))
    if max_val > 500:
        target = 20000.0
        ratio = target / max_val
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
        return result.get("text", "").strip()

    filtered = []
    for w in words:
        word = w.get("word", "")
        conf = w.get("conf", 0)
        # короткие слова (1-2 буквы) с низкой уверенностью — почти всегда мусор
        if len(word) <= 2 and conf < 0.6:
            continue
        if conf >= CONFIDENCE_THRESHOLD:
            filtered.append(word)

    return " ".join(filtered)


def apply_autocorrect(text: str) -> str:
    """Автозамена типичных ошибок распознавания."""
    result = text
    for wrong, correct in AUTOCORRECT.items():
        result = re.sub(rf"(?i)\b{re.escape(wrong)}\b", correct, result)
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

    # автозамена ошибок распознавания
    result = apply_autocorrect(t)

    # голосовые команды пунктуации
    for cmd, sym in PUNCT.items():
        result = re.sub(rf"(?i)\b{re.escape(cmd)}\b", sym, result)

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
        raw = bytes(indata)
        # фильтр тишины — не отправляем пустые блоки в модель
        if is_silence(raw):
            return
        # подавление шума — убираем фоновый гул
        denoised = reduce_noise(raw)
        # нормализация громкости
        normalized = normalize_audio(denoised)
        self.q.put(normalized)

    def _record_session(self):
        print(">>> ЗАПИСЬ...  (Alt+X для остановки)")
        rec = vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
        rec.SetWords(True)              # включаем уверенность по словам

        last_partial = ""
        chars_typed = [0]
        text_buffer = []                # буфер для склейки коротких фраз
        buffer_timer = time.time()

        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                               dtype="int16", channels=1,
                               callback=self._audio_cb):
            while self.recording:
                try:
                    data = self.q.get(timeout=0.2)
                except queue.Empty:
                    # если в буфере есть текст и прошло больше 0.8 сек — отправляем
                    if text_buffer and (time.time() - buffer_timer) > 0.8:
                        merged = " ".join(text_buffer)
                        text_buffer.clear()
                        print(f"\r  → {merged}           ")
                        process_text(merged, chars_typed)
                    continue

                if rec.AcceptWaveform(data):
                    res = json.loads(rec.Result())
                    text = filter_by_confidence(res)

                    if text:
                        # короткие фрагменты (1-2 слова) копим в буфер
                        word_count = len(text.split())
                        if word_count <= 2:
                            text_buffer.append(text)
                            buffer_timer = time.time()
                        else:
                            # длинная фраза — сначала сбрасываем буфер, потом её
                            if text_buffer:
                                text_buffer.append(text)
                                merged = " ".join(text_buffer)
                                text_buffer.clear()
                                print(f"\r  → {merged}           ")
                                process_text(merged, chars_typed)
                            else:
                                print(f"\r  → {text}           ")
                                process_text(text, chars_typed)
                    last_partial = ""
                else:
                    partial = json.loads(rec.PartialResult()).get("partial", "")
                    if partial and partial != last_partial:
                        print(f"\r  … {partial}   ", end="", flush=True)
                        last_partial = partial

            # финальный результат
            res = json.loads(rec.FinalResult())
            text = filter_by_confidence(res)
            if text:
                text_buffer.append(text)

            # сбрасываем оставшийся буфер
            if text_buffer:
                merged = " ".join(text_buffer)
                print(f"\r  → {merged}           ")
                process_text(merged, chars_typed)

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
