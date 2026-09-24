#!/usr/bin/env python3
"""Mätning av transkriberingskedjan utan Pico: syntetiskt svenskt tal via `say`
→ 48 kHz wav (som micken) → whisper-server → jämför ord. Kör:
    .venv/bin/python check_whisper.py [--model ...]
"""
import argparse
import difflib
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ptt_bridge as pb  # noqa: E402

MENINGAR = [
    "Hej, det här är ett test av mikrofonen.",
    "Åsa åkte till Örebro på måndag.",
    "Skicka fakturan till kunden i morgon bitti.",
]
ENGLISH_SENTENCES = [
    "Okay, testing this in English. I would like to continue. This is not English.",
    "Switch this to English. It is still using Swedish instead of English.",
    "Please make the button larger and save the changes.",
]


def norm(s):
    # bokstäver bara: "i morgon"/"imorgon" och skiljetecken ska inte räknas som fel
    return "".join(re.findall(r"\w", s.lower()))


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--model")
    ap.add_argument("--language", choices=("sv", "en"), default="sv")
    ap.add_argument("--voice")
    args = ap.parse_args()
    model, port = pb.recognition_defaults(args.language)
    args.model = args.model or model
    args.voice = args.voice or ("Daniel" if args.language == "en" else "Alva")
    sentences = ENGLISH_SENTENCES if args.language == "en" else MENINGAR
    fel = []
    tmp = tempfile.mkdtemp()
    with pb.WhisperServer(args.model, port=port + 1, language=args.language) as server:
        for i, mening in enumerate(sentences):
            aiff = os.path.join(tmp, f"{i}.aiff")
            wav = os.path.join(tmp, f"{i}.wav")
            subprocess.run(["say", "-v", args.voice, "-o", aiff, mening], check=True)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", aiff,
                            "-ar", str(pb.RATE), "-ac", "1", "-sample_fmt", "s16", wav], check=True)
            data = open(wav, "rb").read()
            t0 = time.monotonic()
            text = pb.clean_text(server.transcribe(data))
            dt = time.monotonic() - t0
            traff = difflib.SequenceMatcher(None, norm(mening), norm(text)).ratio()
            print(f"  {dt:4.1f} s  likhet {traff*100:3.0f} %  {text!r}")
            if traff < 0.9:
                fel.append(f"mening {i}: likhet {traff*100:.0f} %: {text!r} (ville {mening!r})")
            if dt > 10:
                fel.append(f"mening {i}: {dt:.1f} s är för långsamt")
    if fel:
        print("FEL:")
        for f in fel:
            print(" -", f)
        sys.exit(1)
    print("Transkriberingskedjan OK")


if __name__ == "__main__":
    main()
