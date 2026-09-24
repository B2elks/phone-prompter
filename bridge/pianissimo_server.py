#!/usr/bin/env python3
"""HTTP-skal runt Pianissimo-sv, med whisper-servers gränssnitt.

Bryggan pratar HTTP med sin transkriberare: POST /inference med en wav i
ett multipart-fält "file", svar {"text": "..."}. Den här tjänsten svarar
likadant, så bryggan inte behöver veta vilken modell som arbetar.

Modellen (KlangAI/pianissimo-sv) är en FastConformer/TDT via NeMo och kan
inte köras i whisper.cpp. Den laddas en gång vid start — det tar omkring
tio sekunder — och hålls varm.

Körs i NeMo-miljön:  .venv-nemo/bin/python pianissimo_server.py --port 8179

NeMo importeras först i main(), så tolknings- och ljudfunktionerna går att
testa med vanlig python utan hela beroendekedjan.
"""
import argparse
import io
import json
import sys
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELL = "KlangAI/pianissimo-sv"
MODELL_RATE = 16000


def extrahera_wav(body: bytes, content_type: str):
    """Plockar ut fältet "file" ur en multipart-kropp.

    Ljuddata är binärt och innehåller alla byte-värden, så kroppen får
    aldrig avkodas som text.
    """
    if "boundary=" not in (content_type or ""):
        return None
    grans = ("--" + content_type.split("boundary=", 1)[1].strip()).encode()
    for del_ in body.split(grans):
        huvud, _, innehall = del_.partition(b"\r\n\r\n")
        if b'name="file"' in huvud:
            return innehall[:-2] if innehall.endswith(b"\r\n") else innehall
    return None


def wav_till_prov(wav: bytes):
    """Wav-byte till mono float32 vid 16 kHz, som modellen vill ha."""
    import numpy as np

    with wave.open(io.BytesIO(wav)) as w:
        kanaler, bredd, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        rader = w.readframes(w.getnframes())

    typ = {1: np.int8, 2: np.int16, 4: np.int32}[bredd]
    prov = np.frombuffer(rader, dtype=typ).astype(np.float32)
    prov /= float(np.iinfo(typ).max)          # normalisera till [-1, 1]
    if kanaler > 1:
        prov = prov.reshape(-1, kanaler).mean(axis=1)
    if rate != MODELL_RATE:
        prov = sampla_om(prov, rate, MODELL_RATE)
    return prov, MODELL_RATE


def sampla_om(prov, fran, till):
    """Samplar om till modellens frekvens.

    Använder librosa när den finns — den filtrerar bort vikning vid
    nedsampling, vilket 48 kHz från micken behöver. Utan librosa görs en
    enklare linjär interpolation, så funktionen går att testa i en miljö
    utan hela NeMo-kedjan.
    """
    import numpy as np

    try:
        import librosa
    except ImportError:
        n = int(round(len(prov) * till / fran))
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        gamla = np.arange(len(prov), dtype=np.float64)
        nya = np.linspace(0, len(prov) - 1, n)
        return np.interp(nya, gamla, prov).astype(np.float32)
    return librosa.resample(prov, orig_sr=fran, target_sr=till)


def json_svar(text: str) -> bytes:
    return json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")


def bygg_handler(modell, log=print):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass                              # tyst; vi loggar det vi bryr oss om

        def do_POST(self):
            if not self.path.startswith("/inference"):
                self.send_error(404)
                return
            langd = int(self.headers.get("Content-Length", 0))
            wav = extrahera_wav(self.rfile.read(langd), self.headers.get("Content-Type", ""))
            if wav is None:
                self.send_error(400, "ingen wav i fältet 'file'")
                return
            try:
                prov, _ = wav_till_prov(wav)
                ut = modell.transcribe([prov], verbose=False)
                text = ut[0].text if hasattr(ut[0], "text") else str(ut[0])
            except Exception as fel:           # noqa: BLE001
                # En trasig inspelning får inte ta ner tjänsten; bryggan
                # ska få ett svar och kunna gå vidare.
                log(f"pianissimo: transkribering misslyckades: {fel}")
                self.send_error(500, "transkribering misslyckades")
                return
            kropp = json_svar(text.strip())
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(kropp)))
            self.end_headers()
            self.wfile.write(kropp)

        def do_GET(self):
            # Bryggan kollar liveness med en TCP-anslutning, men en hälsoväg
            # gör det möjligt att se att modellen är laddad.
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pianissimo redo\n")

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8179)
    ap.add_argument("--modell", default=MODELL)
    args = ap.parse_args()

    import warnings
    warnings.filterwarnings("ignore")
    print(f"laddar {args.modell} ...", flush=True)
    import nemo.collections.asr as nemo_asr
    modell = nemo_asr.models.ASRModel.from_pretrained(args.modell)
    modell.eval()
    print(f"pianissimo redo på :{args.port}", flush=True)

    HTTPServer(("127.0.0.1", args.port), bygg_handler(modell)).serve_forever()


if __name__ == "__main__":
    main()
