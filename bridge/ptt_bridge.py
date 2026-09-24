#!/usr/bin/env python3
"""Push-to-talk-brygga: Pico-knapp → inspelning från USB-micken → KB-Whisper
(whisper.cpp-server) → tangentbordsinmatning.

Texten kommer fras för fras medan luren är lyft: Segmenter klipper vid
~0,5 s paus (eller 8 s tal), en arbetstråd transkriberar och skriver i
ordning, och "klart" spelas när sista frasen är skriven.

Kärnan (Bridge, Segmenter, wav_bytes, multipart, clean_text) har inga
beroenden och testas i test_bridge.py. Adaptrarna (pyserial, sounddevice, pynput) laddas
först i main().

  python3 bridge/ptt_bridge.py --model models/ggml-kb-whisper-medium-q5_0.bin
  python3 bridge/ptt_bridge.py --dry-run     # skriver texten i terminalen i stället
"""
import argparse
import array
import io
import json
import math
import os
import queue
import re
import socket
import struct
import subprocess
import sys
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave

USB_VID = 0xCAFE
USB_PID = 0x4011
MIC_NAME = "Pico INMP441 Mic"
RATE = 48000
DEFAULT_PORT = 8178
PIANISSIMO_PORT = 8179
PIANISSIMO_VENV = str(Path(__file__).with_name(".venv-nemo") / "bin" / "python")
ENGLISH_PORT = 8180


def recognition_defaults(language):
    # English must not reuse the Swedish-finetuned model on the shared port.
    name, port = (("ggml-medium.en-q5_0.bin", ENGLISH_PORT) if language == "en" else
                  ("ggml-kb-whisper-medium-q5_0.bin", DEFAULT_PORT))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", name), port


# ------------------------------------------------------------------ kärna

def trimma_svans(pcm16: bytes, rate: int, ms: int) -> bytes:
    """Tar bort de sista millisekunderna ur ett ljudklipp.

    Luren som läggs i klykan hörs som ett kort klingande sist i inspelningen.
    Whisper tolkar det inte som tyst utan hittar på undertextar-eftertexter:
    i loggen kom 8 av 9 sådana i sista frasen före "pålagt", med bara
    170–400 ms "tal" i segmentet.

    200 ms visade sig för lite: en svensk session med trimningen aktiv fick
    ändå "Text: Britt Borg" ur ett segment med 170 ms tal. Höjt till 400 ms,
    vilket täcker hela spannet som observerats.

    Längden räknas i tid, inte byte, så den blir densamma för mickens 48 kHz
    som för telefonens 8 kHz. Resultatet hålls jämnt så 16-bitars prov
    aldrig delas mitt itu.
    """
    if ms <= 0 or not pcm16:
        return pcm16
    bort = int(rate * 2 * ms / 1000) & ~1
    kvar = len(pcm16) - bort
    if kvar <= 0:
        return b""
    return pcm16[:kvar & ~1]


def wav_bytes(pcm16: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)
    return buf.getvalue()


def multipart(fields: dict, file_field: str, filename: str, data: bytes):
    boundary = "----phone-prompter-" + uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
              f"Content-Type: audio/wav\r\n\r\n".encode())
    out.write(data)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", out.getvalue()


_ARTEFAKT = re.compile(r"^\s*[\[(].*?[\])]\s*$")  # "[BLANK_AUDIO]", "(musik)"


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if _ARTEFAKT.match(text):
        return ""
    return text


# Only standalone/trailing credit sentences, never arbitrary URLs or mentions.
# Undertextar-eftertexter som Whisper hittar på när ljudet tar slut. Modellen
# är tränad på undertextat material och fyller tystnad med eftertexter.
#
# Två fall, avsiktligt olika strikta:
#   1. Med inledning ("Textning: …") får eftertexten ha ett efterhäng,
#      eftersom "Textning: Svensk Medietext för SVT" är en hel kreditrad.
#   2. Utan inledning måste markören avsluta meningen. Annars skulle
#      "britta.sdmedia.se och så vidare." ätas upp — och det är riktig
#      diktering som ska bevaras.
#
# En känd MARKÖR krävs alltid. Att matcha enbart "Textning:" vore för brett:
# "vi behöver textning av filmen" skulle förlora sin mening. Markörerna är
# hämtade ur 459 verkliga transkriberingar, där 7 innehöll eftertexter.
_KREDIT_MARKOR = (
    r"(?:www\.)?(?:britta\.)?sdi?media\.(?:se|com)"
    r"|btistudios\.com|amara\.org"
    r"|svensk\s+medietext"
    r"|whisper\s*[,–:-]\s*kungliga\s+biblioteket"
    r"|\[\s*almars\s+stundar\s*:?\s*\]?"
)
_INLEDNING = r"(?:textning|undertextning|undertexter|översättning|text)\s*[:–-]?\s*(?:av\s+)?"
_DICTATION_CREDIT = re.compile(
    r"(?:^|(?<=[.!?])\s+)(?:"
    rf"{_INLEDNING}[^!?]{{0,40}}?(?:{_KREDIT_MARKOR})[^!?]{{0,40}}"
    rf"|(?:{_KREDIT_MARKOR})"
    r")[.!?]*\s*$", re.IGNORECASE)


def clean_dictation_text(text: str) -> str:
    text = clean_text(text)
    while True:
        cleaned = _DICTATION_CREDIT.sub('', text).strip()
        if cleaned == text:
            return text
        text = cleaned


def parse_inference_json(body: bytes) -> str:
    return json.loads(body.decode("utf-8")).get("text", "")


def valj_transkriberare(args, log=print):
    """Transkriberare enligt --engine. Startar inget förrän den används."""
    if getattr(args, "engine", "whisper") == "pianissimo":
        return PianissimoServer(port=args.pianissimo_port, language=args.language,
                                venv=args.pianissimo_venv, log=log)
    return WhisperServer(args.model, port=args.port, threads=args.threads,
                         language=args.language, log=log)


def pick_port(ports):
    for p in ports:
        if getattr(p, "vid", None) == USB_VID and getattr(p, "pid", None) == USB_PID:
            return p.device
    return None


class Segmenter:
    """Klipper en PCM-ström i fraser på pauser. Energibaserad med adaptiv
    brusnivå: tal = block-RMS > max(min_thresh, 3 × brusgolv)."""

    BLOCK_MS = 10

    def __init__(self, rate=RATE, min_silence_ms=350, max_segment_ms=8000, min_speech_ms=150,
                 pre_roll_ms=200, min_thresh=250, speech_ratio=0.25, min_cut_ms=600,
                 long_silence_ms=1000, trailing_silence_ms=None):
        self.rate = rate
        self.block = rate * self.BLOCK_MS // 1000
        self.min_silence = min_silence_ms
        self.max_segment = max_segment_ms
        self.min_speech = min_speech_ms
        self.pre_roll_blocks = pre_roll_ms // self.BLOCK_MS
        self.min_thresh = min_thresh
        self.speech_ratio = speech_ratio
        self.min_cut = min_cut_ms   # så mycket tal krävs innan en paus får klippa (annars ordhack)
        # …men en riktigt lång paus är en meningsgräns oavsett hur mycket tal
        # som hunnit räknas. Utan detta växte segment med tyst tal till
        # maxlängden och kapade mitt i meningen.
        self.long_silence = long_silence_ms
        self.trailing_silence_ms = trailing_silence_ms
        self.reset()

    def reset(self):
        self.tail = b""
        self.pre = []           # senaste tysta block före tal
        self.seg = []           # block i pågående segment
        self.speech_ms = 0
        self.silence_ms = 0
        self.longest_silence = 0
        self.floor = None
        self.level = None       # löpande talnivå (RMS)
        self.rms_hist = []
        self.recent = []        # senaste block-RMS för utjämning (30 ms)
        self.peak = 0
        self.last_info = ""

    def _rms(self, blk):
        a = array.array("h", blk)
        return math.sqrt(sum(v * v for v in a) / len(a)) if len(a) else 0.0

    def _thresh(self):
        t = max(self.min_thresh, 3 * self.floor)
        if self.level:
            t = max(t, self.level * self.speech_ratio)  # relativt talnivån: andning/lurbrus räknas som paus
        return t

    def _is_speech(self, rms):
        if self.floor is None:
            self.floor = min(rms, self.min_thresh)
        self.rms_hist.append(rms)
        self.recent = (self.recent + [rms])[-3:]
        rms = sum(self.recent) / len(self.recent)   # 30 ms utjämning mot fladder
        speech = rms > self._thresh()
        if speech:
            self.level = rms if self.level is None else self.level + (rms - self.level) * 0.05
        else:  # golvet följer bara tysta block: sjunker direkt, stiger långsamt
            self.floor = rms if rms < self.floor else self.floor + (rms - self.floor) * 0.05
        return speech

    def push(self, pcm):
        out = []
        data = self.tail + pcm
        nblk = len(data) // (self.block * 2)
        self.tail = data[nblk * self.block * 2:]
        for i in range(nblk):
            blk = data[i * self.block * 2:(i + 1) * self.block * 2]
            self.peak = max(self.peak, max(abs(v) for v in array.array("h", blk)))
            speech = self._is_speech(self._rms(blk))
            if not self.seg:
                if speech:
                    self.seg = self.pre + [blk]
                    self.pre = []
                    self.speech_ms = self.BLOCK_MS
                    self.silence_ms = 0
                else:
                    self.pre.append(blk)
                    if len(self.pre) > self.pre_roll_blocks:
                        self.pre.pop(0)
                continue
            self.seg.append(blk)
            if speech:
                self.speech_ms += self.BLOCK_MS
                self.silence_ms = 0
            else:
                self.silence_ms += self.BLOCK_MS
                self.longest_silence = max(self.longest_silence, self.silence_ms)
            seg_ms = len(self.seg) * self.BLOCK_MS
            pause_cut = (self.silence_ms >= self.min_silence
                         and self.speech_ms >= self.min_cut)
            if self.silence_ms >= self.long_silence and self.speech_ms:
                pause_cut = True
            if pause_cut or seg_ms >= self.max_segment:
                done = self._cut()
                if done:
                    out.append(done)
        return out

    def _cut(self):
        seg, self.seg = self.seg, []
        ok = self.speech_ms >= self.min_speech
        if self.trailing_silence_ms is not None:
            trim = max(0, (self.silence_ms - self.trailing_silence_ms) // self.BLOCK_MS)
            if trim:
                seg = seg[:-trim]
        h = sorted(self.rms_hist)
        pct = lambda q: h[min(len(h) - 1, int(q * len(h)))] if h else 0
        self.last_info = (f"tal {self.speech_ms} ms, längsta tystnad {self.longest_silence} ms, "
                          f"golv {self.floor:.0f}, nivå {self.level or 0:.0f}, tröskel {self._thresh():.0f}, "
                          f"rms p10/p50/p90 {pct(.1):.0f}/{pct(.5):.0f}/{pct(.9):.0f}, topp {self.peak}")
        self.rms_hist = []
        self.peak = 0
        self.speech_ms = 0
        self.silence_ms = 0
        self.longest_silence = 0
        return b"".join(seg) if ok else None

    def pagaende(self):
        """Ljudet i frasen som just nu byggs, utan att ändra tillståndet.

        Demoskärmen transkriberar detta medan man talar för att visa
        preliminär text. Bufferten får inte konsumeras — frasen är inte klar.
        """
        return b"".join(self.seg)

    def flush(self):
        if self.tail and self.seg:
            self.seg.append(self.tail)
        self.tail = b""
        self.pre = []
        return self._cut() if self.seg else None


class Bridge:
    """Tillståndsmaskin: rader från Pico in, fraser ut via arbetstråd."""

    def __init__(self, link, recorder, transcriber, typer, trailing_space=True, log=print,
                 sounds=True, segmenter=None, enter_on_hangup=True, trim_tail_ms=400,
                 demo=None, demo_intervall=0.5):
        self.enter_on_hangup = enter_on_hangup
        self.trim_tail_ms = trim_tail_ms
        self.demo = demo
        self.demo_intervall = demo_intervall
        self.demo_nasta = 0.0
        self.link = link
        self.recorder = recorder
        self.transcriber = transcriber
        self.typer = typer
        self.trailing_space = trailing_space
        self.log = log
        self.sounds = sounds
        self.seg = segmenter or Segmenter(rate=recorder.rate)
        self.seg.trailing_silence_ms = 200
        self.recording = False
        self.busy_sent = False
        self.jobs = queue.Queue()
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    # --- rader från Pico
    def handle_line(self, line):
        line = line.strip()
        if line in ("DOWN", "HELLO DOWN"):
            self._start()
        elif line == "UP":
            self._finish()
        elif line == "HELLO UP":
            if self.recording:
                self.recorder.stop()
                self.recording = False
                self.seg.reset()
        elif line in ("PONG", "PLAYING"):
            pass
        elif line == "NOCLIP":
            self.log("pico: klippet saknas i firmwaren")
        elif line:
            self.log("pico: okänt", line)

    # --- anropas ofta från huvudloopen: hämtar nytt ljud och klipper fraser
    def poll(self):
        if not self.recording:
            return
        pcm = self.recorder.take()
        if pcm:
            for segment in self.seg.push(pcm):
                self._enqueue(segment)
        if self.demo:
            self._demo_preliminar()

    def _demo_preliminar(self):
        """Visar halvfärdig text på demoskärmen medan frasen pågår.

        Transkriberingen tar ungefär lika lång tid oavsett ljudlängd, så att
        köra om den växande frasen några gånger per sekund är billigt. Texten
        får vara fel — skärmen byter ut den, till skillnad från tangentbordet.
        """
        nu = time.monotonic()
        if nu < self.demo_nasta:
            return
        self.demo_nasta = nu + self.demo_intervall
        pcm = self.seg.pagaende()
        if len(pcm) < self.recorder.rate:          # under en halv sekund: för lite
            return
        try:
            text = clean_dictation_text(
                self.transcriber.transcribe(wav_bytes(pcm, self.recorder.rate)))
        except Exception:                          # noqa: BLE001
            return                                 # demot får aldrig störa dikteringen
        if text:
            self.demo.preliminar(text)

    def drain(self):
        """Väntar tills alla köade fraser är klara (tester och avslut)."""
        self.jobs.join()

    def _demo_lage(self, lyssnar):
        if self.demo:
            self.demo.lage(lyssnar)

    def _start(self):
        if self.recording:
            return
        self.seg.reset()
        self.recorder.start()
        self.recording = True
        self.text_seen = False
        self.demo_nasta = 0.0
        self._demo_lage(True)
        self.busy_sent = False
        self.log("lyft: lyssnar")

    def _finish(self):
        if not self.recording:
            return
        self.recorder.stop()
        self.recording = False
        self._demo_lage(False)
        pcm = trimma_svans(self.recorder.take(), self.recorder.rate, self.trim_tail_ms)
        for segment in self.seg.push(pcm):
            self._enqueue(segment)
        rest = self.seg.flush()
        if rest:
            self._enqueue(rest)
        self.jobs.put(("end", None))

    def _enqueue(self, segment):
        if not self.busy_sent:
            self.link.send("BUSY")
            self.busy_sent = True
        self.jobs.put(("seg", segment))

    def _work(self):
        while True:
            kind, segment = self.jobs.get()
            try:
                if kind == "seg":
                    self._transcribe(segment)
                else:
                    if self.enter_on_hangup and self.text_seen:
                        self.typer.key("enter")
                    self.link.send("IDLE")
                    if self.sounds:
                        self.link.send("PLAY klart" if self.text_seen else "PLAY inget")
                    self.log("pålagt")
            except Exception as e:
                self.log("arbetstråd:", e)
            finally:
                self.jobs.task_done()

    def _transcribe(self, segment):
        seconds = len(segment) / 2 / self.recorder.rate
        t0 = time.monotonic()
        try:
            text = clean_dictation_text(self.transcriber.transcribe(wav_bytes(segment, self.recorder.rate)))
        except Exception as e:
            self.log("transkribering föll:", e)
            return
        self.log(f"{seconds:.1f} s → {time.monotonic() - t0:.1f} s: {text!r}  [{self.seg.last_info}]")
        if text:
            self.typer.type(text + (" " if self.trailing_space else ""))
            self.text_seen = True


# ------------------------------------------------------------------ adaptrar

class HttpTranskriberare:
    """Gemensamt för transkriberare som körs som lokal HTTP-tjänst.

    Bryggan bryr sig bara om transcribe(); varifrån texten kommer är
    underordnat. Underklasser startar sin egen process i __enter__.
    """

    port = None
    proc = None

    def _alive(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                return True
        except OSError:
            return False

    def _vanta_pa_start(self, cmd, namn, sekunder=120):
        for _ in range(sekunder * 10):
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"{namn} avslutades direkt (kod {self.proc.returncode}); "
                    f"kör kommandot för hand: {' '.join(str(c) for c in cmd)}")
            if self._alive():
                return True
            time.sleep(0.1)
        raise RuntimeError(f"{namn} svarade inte inom {sekunder} s")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def transcribe(self, wav: bytes) -> str:
        ctype, body = multipart({"response_format": "json", "language": self.language,
                                "temperature": "0.0", "temperature_inc": "0.2"},
                                "file", "clip.wav", wav)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/inference", data=body,
                                     headers={"Content-Type": ctype}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            return parse_inference_json(r.read())


class PianissimoServer(HttpTranskriberare):
    """Svensk FastConformer/TDT via NeMo, bakom whisper-serverns gränssnitt.

    Modellen kan inte köras i whisper.cpp och behöver en egen Python-miljö
    med NeMo. Den laddas en gång vid start (omkring tio sekunder) och hålls
    varm; bryggan märker ingen skillnad mot whisper-server.
    """

    def __init__(self, port=PIANISSIMO_PORT, language="sv", venv=None, log=print):
        if language != "sv":
            sys.exit("pianissimo-sv kan bara svenska — kör utan --engine pianissimo "
                     f"för {language}")
        self.port = port
        self.language = language
        self.log = log
        self.venv = Path(venv or PIANISSIMO_VENV)
        self.skript = Path(__file__).with_name("pianissimo_server.py")
        self.proc = None

    def __enter__(self):
        if self._alive():
            self.log(f"pianissimo svarar redan på :{self.port}")
            return self
        if not self.venv.exists():
            sys.exit(f"NeMo-miljön saknas: {self.venv}\nSe README: pianissimo kräver "
                     "en egen venv på Python 3.11.")
        cmd = [str(self.venv), str(self.skript), "--port", str(self.port)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.log("startar pianissimo (laddar modellen, ~10 s) …")
        self._vanta_pa_start(cmd, "pianissimo")
        self.log(f"pianissimo igång på :{self.port}")
        return self


class WhisperServer(HttpTranskriberare):
    """Startar whisper-server med modellen laddad en gång och pratar HTTP med den."""

    def __init__(self, model, port=DEFAULT_PORT, threads=4, language="sv", binary="whisper-server", log=print):
        self.model = model
        self.port = port
        self.threads = threads
        self.language = language
        self.binary = binary
        self.log = log
        self.proc = None

    def __enter__(self):
        if self._alive():
            self.log(f"whisper-server svarar redan på :{self.port}")
            return self
        cmd = [self.binary, "-m", self.model, "-l", self.language, "-t", str(self.threads),
               "--host", "127.0.0.1", "--port", str(self.port), "--convert", "--tmp-dir", "/tmp", "-nt"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(600):
            if self.proc.poll() is not None:
                raise RuntimeError(f"whisper-server avslutades direkt (kod {self.proc.returncode}); kör kommandot för hand: {' '.join(cmd)}")
            if self._alive():
                self.log(f"whisper-server igång på :{self.port} med {os.path.basename(self.model)}")
                return self
            time.sleep(0.1)
        raise RuntimeError("whisper-server svarade inte inom 60 s")


class SerialLink:
    def __init__(self, device, log=print):
        import serial  # pyserial
        self.ser = serial.Serial(device, 115200, timeout=0.1)
        self.log = log
        self.lock = threading.Lock()

    def send(self, line):
        with self.lock:
            self.ser.write((line + "\n").encode())

    def lines(self):
        buf = b""
        while True:
            chunk = self.ser.read(64)
            if not chunk:
                yield None  # timeout-tick så anroparen kan göra annat
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.decode("utf-8", "replace").strip("\r")


class SoundDeviceRecorder:
    """Öppnar mikrofonen bara medan luren är lyft."""

    def __init__(self, device_name=MIC_NAME, rate=RATE):
        import sounddevice as sd
        self.sd = sd
        self.rate = rate
        idx = next((i for i, d in enumerate(sd.query_devices())
                    if d["name"] == device_name and d["max_input_channels"] >= 1), None)
        if idx is None:
            raise RuntimeError(f"ljudenheten {device_name!r} hittades inte: {[d['name'] for d in sd.query_devices()]}")
        self.lock = threading.Lock()
        self.chunks = []
        self.capturing = False
        self.device = idx
        self.stream = None

    def _cb(self, indata, frames, t, status):
        with self.lock:
            if self.capturing:
                self.chunks.append(bytes(indata))

    def start(self):
        if self.stream is not None:
            return
        with self.lock:
            self.chunks = []
            self.capturing = True
        try:
            self.stream = self.sd.InputStream(device=self.device, samplerate=self.rate,
                                             channels=1, dtype="int16", blocksize=480,
                                             callback=self._cb)
            self.stream.start()
        except Exception:
            self.stop()
            raise

    def take(self):
        with self.lock:
            data = b"".join(self.chunks)
            self.chunks = []
        return data

    def stop(self):
        stream, self.stream = self.stream, None
        try:
            if stream is not None:
                try:
                    stream.stop()
                finally:
                    stream.close()
        finally:
            with self.lock:
                self.capturing = False


class PynputTyper:
    def __init__(self, delay_ms=20):
        from pynput.keyboard import Controller, Key
        self.kb = Controller()
        self.Key = Key
        self.delay = delay_ms / 1000

    def type(self, text):
        for ch in text:  # tecken för tecken med paus, så mottagaren hinner med
            self.kb.type(ch)
            if self.delay:
                time.sleep(self.delay)

    def key(self, name):
        k = getattr(self.Key, name)
        self.kb.press(k)
        self.kb.release(k)


class PrintTyper:
    def type(self, text):
        print(f"[skulle skriva] {text!r}")

    def key(self, name):
        print(f"[skulle trycka] {name}")


def find_serial_port():
    from serial.tools import list_ports
    return pick_port(list_ports.comports())


MULTICAST_GRUPP = "239.1.1.10"
MULTICAST_PORT = 5004
MULTICAST_KALLA = "192.168.1.215"      # telefonens IP; andra avsändare ignoreras
MULTICAST_LOKAL = ""                   # tomt = alla gränssnitt (INADDR_ANY)
MULTICAST_TYSTNAD_MS = 800


def fel_typer(multicast_lage):
    """Undantag huvudloopen ska fånga.

    pyserial behövs inte i multicast-läge, så det får inte importeras då —
    annars kraschar bryggan på en maskin utan Pico-beroenden.
    """
    if multicast_lage:
        return (OSError,)
    import serial
    return (serial.SerialException, OSError)


def bygg_argparser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="modellfil (väljs annars utifrån språk)")
    ap.add_argument("--port", type=int, help="whisper-server-port (sv: 8178, en: 8180)")
    ap.add_argument("--serial", help="serieport (annars hittas Pico:n via VID/PID)")
    ap.add_argument("--pause", type=float, default=0.35, help="paus i sekunder som avslutar en fras")
    ap.add_argument("--max-phrase", type=float, default=8.0,
                    help="längsta fras i sekunder; säkerhetsspärr när ingen "
                         "paus kommer — normalt klipper pausen först")
    ap.add_argument("--long-pause", type=float, default=1.0,
                    help="paus i sekunder som alltid avslutar en fras, även om "
                         "lite tal hunnit räknas")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--language", choices=("sv", "en"), default="sv", help="språk för transkribering")
    ap.add_argument("--dry-run", action="store_true", help="skriv i terminalen i stället för som tangentbord")
    ap.add_argument("--no-trailing-space", action="store_true")
    ap.add_argument("--no-sounds", action="store_true", help="spela inte 'klart'/'inget' i luren")
    ap.add_argument("--no-enter", action="store_true", help="tryck inte Enter när luren läggs på")
    ap.add_argument("--type-delay-ms", type=float, default=20, help="paus mellan tecken vid skrivning")
    ap.add_argument("--test-press", type=float, metavar="SEK",
                    help="emulera ett knapptryck på SEK sekunder via PRESS/RELEASE och avsluta (test utan lödd knapp)")
    ap.add_argument("--demo", action="store_true",
                    help="visa det du säger som levande undertext i webbläsaren")
    ap.add_argument("--demo-port", type=int, default=8790)
    ap.add_argument("--engine", choices=("whisper", "pianissimo"), default="whisper",
                    help="transkriberare; pianissimo är en svensk FastConformer "
                         "som inte hittar på text ur tystnad (endast svenska)")
    ap.add_argument("--pianissimo-port", type=int, default=PIANISSIMO_PORT)
    ap.add_argument("--pianissimo-venv", default=PIANISSIMO_VENV,
                    help="python i NeMo-miljön")
    ap.add_argument("--trim-tail-ms", type=int, default=400,
                    help="millisekunder som klipps bort sist i inspelningen; "
                         "luren i klykan hörs annars som tal (0 = av)")
    ap.add_argument("--multicast", action="store_true",
                    help="ta ljudet från telefonens multicast-paging i stället för Pico-micken")
    ap.add_argument("--multicast-grupp", default=MULTICAST_GRUPP)
    ap.add_argument("--multicast-port", type=int, default=MULTICAST_PORT)
    ap.add_argument("--multicast-kalla", default=MULTICAST_KALLA,
                    help="telefonens IP; paket från andra avsändare ignoreras")
    ap.add_argument("--multicast-lokal", default=MULTICAST_LOKAL,
                    help="gränssnitt att gå med i gruppen på; tomt = alla")
    ap.add_argument("--multicast-tystnad-ms", type=int, default=MULTICAST_TYSTNAD_MS,
                    help="uppehåll som avslutar en fras")
    return ap


def main():
    ap = bygg_argparser()
    args = ap.parse_args()
    default_model, default_port = recognition_defaults(args.language)
    args.model = args.model if args.model is not None else default_model
    args.port = args.port if args.port is not None else default_port

    if not os.path.exists(args.model):
        sys.exit(f"modellen saknas: {args.model}\nKör bridge/setup.sh för att hämta talmodellerna.")
    dev = None
    if not args.multicast:
        dev = args.serial or find_serial_port()
        if not dev:
            sys.exit("ingen Pico-mick hittad (VID 0xCAFE PID 0x4011) — är firmwaren flashad och inkopplad?")

    log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
    log(f"taligenkänning: språk={args.language}, modell={os.path.basename(args.model)}, port={args.port}")
    typer = PrintTyper() if args.dry_run else PynputTyper(delay_ms=args.type_delay_ms)
    skarm = None
    if args.demo:
        import demo as demomodul
        skarm = demomodul.Demoskarm(port=args.demo_port, log=log).starta()
        skarm.lage(False, kalla="bordstelefon" if args.multicast else "Pico-mick")
        typer = demomodul.DemoTyper(typer, skarm)
    if not args.dry_run:
        from Quartz import CGPreflightPostEventAccess
        log(f"tangentbordsbehörighet: {'OK' if CGPreflightPostEventAccess() else 'SAKNAS'}")
    with valj_transkriberare(args, log=log) as server:
        if args.multicast:
            import multicast as mc
            strom = mc.PaketStrom(args.multicast_grupp, args.multicast_port,
                                  args.multicast_lokal, args.multicast_kalla, log=log)
            strom.start()
            recorder = mc.MulticastRecorder(strom)
            link = mc.MulticastLink(strom, tystnad_ms=args.multicast_tystnad_ms, log=log)
        else:
            recorder = SoundDeviceRecorder()
            link = SerialLink(dev, log=log)
        # Frekvensen måste följa källan: 48 kHz från micken, 8 kHz från
        # telefonen. Hårdkodad rate klippte fraser på fel längd.
        seg = Segmenter(rate=recorder.rate, min_silence_ms=int(args.pause * 1000),
                        max_segment_ms=int(args.max_phrase * 1000),
                        long_silence_ms=int(args.long_pause * 1000))
        bridge = Bridge(link, recorder, server, typer, trailing_space=not args.no_trailing_space, log=log,
                        sounds=not args.no_sounds, segmenter=seg, enter_on_hangup=not args.no_enter,
                        trim_tail_ms=args.trim_tail_ms, demo=skarm)
        if args.multicast:
            log(f"klar: lyssnar på {args.multicast_grupp}:{args.multicast_port} "
                f"från {args.multicast_kalla}. Lyft luren och tryck paging-knappen.")
        else:
            log(f"klar: serieport {dev}, mick {MIC_NAME!r}. Håll knappen och prata.")
        link.send("PING")
        deadline = None
        if args.test_press:
            log(f"emulerar knapptryck i {args.test_press} s — prata nu")
            link.send("PRESS")
            deadline = time.monotonic() + args.test_press
        fangas = fel_typer(args.multicast)
        try:
            for line in link.lines():
                if line is not None:
                    bridge.handle_line(line)
                bridge.poll()
                if deadline and time.monotonic() >= deadline:
                    link.send("RELEASE")
                    deadline = None
                    done_at = time.monotonic() + 15
                elif deadline is None and args.test_press and not bridge.recording and time.monotonic() > done_at - 14.5:
                    bridge.drain()
                    break
        except KeyboardInterrupt:
            pass
        except fangas as e:
            log("telefonen försvann:", e)   # normal avslutning; menyradsappen startar om vid inkoppling
        finally:
            recorder.stop()


if __name__ == "__main__":
    main()
