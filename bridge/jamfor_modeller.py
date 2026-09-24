#!/usr/bin/env python3
"""Jämför transkriberare på svenskt ljud med känt facit.

Ljudet degraderas till telefonkvalitet — 8 kHz genom G.711 a-law — eftersom
det är vad telefonen faktiskt skickar. En modell kan vara utmärkt på rent
ljud och svag på smalband.

    python3 jamfor_modeller.py --whisper-port 8178 --pianissimo-port 8179
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ptt_bridge as pb


SIFFROR = {"noll": "0", "en": "1", "ett": "1", "två": "2", "tre": "3", "fyra": "4",
           "fem": "5", "sex": "6", "sju": "7", "åtta": "8", "nio": "9", "tio": "10",
           "elva": "11", "tolv": "12"}


def ord_lista(text):
    """Ord för jämförelse, med talord normaliserade till siffror.

    "tryck ett" och "tryck 1" är samma transkribering med olika stil — att
    räkna det som fel gjorde KB-Whisper 2,5 gånger sämre än den var.
    """
    ord = re.findall(r"[a-zåäöé]+|\d+", text.lower())
    return [SIFFROR.get(o, o) for o in ord]


def wer(facit, hypotes):
    a, b = ord_lista(facit), ord_lista(hypotes)
    if not a:
        return 0, 0
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i-1][j] + 1, d[i][j-1] + 1,
                          d[i-1][j-1] + (a[i-1] != b[j-1]))
    return d[len(a)][len(b)], len(a)


def telefonkvalitet(kalla, mal):
    """8 kHz genom G.711 a-law — samma väg som telefonens paging tar."""
    alaw = mal.with_suffix(".alaw")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(kalla),
                    "-ar", "8000", "-ac", "1", "-f", "alaw", str(alaw)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "alaw", "-ar", "8000",
                    "-ac", "1", "-i", str(alaw), str(mal)], check=True)
    alaw.unlink()
    return mal


class Klient(pb.HttpTranskriberare):
    def __init__(self, port, language="sv"):
        self.port, self.language = port, language


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--whisper-port", type=int, default=8178)
    ap.add_argument("--pianissimo-port", type=int, default=8179)
    ap.add_argument("--fall", default="/tmp/testfall.json",
                    help="JSON: {ljudfil: facittext}")
    ap.add_argument("--arbetskatalog", default="/tmp/jamfor")
    args = ap.parse_args()

    fall = json.loads(Path(args.fall).read_text())
    kat = Path(args.arbetskatalog)
    kat.mkdir(parents=True, exist_ok=True)

    motorer = {}
    for namn, port in (("KB-Whisper", args.whisper_port),
                       ("Pianissimo", args.pianissimo_port)):
        k = Klient(port)
        if k._alive():
            motorer[namn] = k
        else:
            print(f"  (hoppar över {namn}: inget svar på :{port})")
    if not motorer:
        sys.exit("ingen transkriberare svarar")

    summa = {n: [0, 0, 0.0] for n in motorer}          # fel, ord, sekunder
    print(f"{len(fall)} klipp, telefonkvalitet 8 kHz a-law\n")

    for i, (ljud, facit) in enumerate(sorted(fall.items()), 1):
        wav = telefonkvalitet(Path(ljud), kat / f"{i:03d}.wav")
        data = wav.read_bytes()
        rader = []
        for namn, k in motorer.items():
            t0 = time.monotonic()
            try:
                text = k.transcribe(data)
            except Exception as fel:                   # noqa: BLE001
                text = f"<fel: {fel}>"
            tid = time.monotonic() - t0
            f, n = wer(facit, text)
            summa[namn][0] += f
            summa[namn][1] += n
            summa[namn][2] += tid
            rader.append((namn, text, f, n, tid))
        print(f"[{i}/{len(fall)}] facit: {facit[:64]}")
        for namn, text, f, n, tid in rader:
            print(f"     {namn:<11} {f}/{n} fel  {tid:4.1f}s  {text[:64]!r}")

    print("\n=== summa ===")
    for namn, (f, n, t) in summa.items():
        print(f"  {namn:<11} {f}/{n} ord fel = {100*f/n if n else 0:.1f} %   "
              f"{t:.0f} s totalt")


if __name__ == "__main__":
    main()
