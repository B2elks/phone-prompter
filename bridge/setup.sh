#!/bin/bash
# Installerar beroenden och svenska/engelska Whisper-modeller (ggml, q5_0).
#   ./setup.sh            # kb-whisper-medium (539 MB)
#   ./setup.sh large      # kb-whisper-large (1,1 GB), bäst kvalitet
set -euo pipefail
cd "$(dirname "$0")"
SIZE="${1:-medium}"

command -v whisper-server >/dev/null || { echo "whisper-server saknas: brew install whisper-cpp"; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg saknas (whisper-server --convert behöver den): brew install ffmpeg"; exit 1; }

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q pyserial sounddevice pynput
echo "venv klar: $(.venv/bin/python -c 'import serial, sounddevice, pynput; print("pyserial", serial.__version__, "sounddevice", sounddevice.__version__)')"

mkdir -p models
MODEL="models/ggml-kb-whisper-$SIZE-q5_0.bin"
if [ ! -s "$MODEL" ]; then
    echo "Hämtar KBLab/kb-whisper-$SIZE (ggml q5_0) → $MODEL"
    curl -L --fail --progress-bar -o "$MODEL.part" \
        "https://huggingface.co/KBLab/kb-whisper-$SIZE/resolve/main/ggml-model-q5_0.bin"
    mv "$MODEL.part" "$MODEL"
fi
# ggml-magic: 'lmgg' (0x67676d6c little-endian)
magic=$(head -c 4 "$MODEL" | xxd -p)
[ "$magic" = "6c6d6767" ] || { echo "Modellen har fel magic ($magic), trasig nedladdning?"; exit 1; }
echo "Modell OK: $(du -h "$MODEL" | cut -f1) $MODEL"
ENGLISH_MODEL="models/ggml-medium.en-q5_0.bin"
if [ ! -s "$ENGLISH_MODEL" ]; then
    echo "Hämtar Whisper medium.en (539 MB) → $ENGLISH_MODEL"
    curl -L --fail --progress-bar -o "$ENGLISH_MODEL.part" \
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en-q5_0.bin"
    mv "$ENGLISH_MODEL.part" "$ENGLISH_MODEL"
fi
magic=$(head -c 4 "$ENGLISH_MODEL" | xxd -p)
[ "$magic" = "6c6d6767" ] || { echo "Engelska modellen har fel magic ($magic)"; exit 1; }
echo "Engelsk modell OK: $ENGLISH_MODEL"
echo
echo "Kör bryggan:  .venv/bin/python ptt_bridge.py --model $MODEL"
echo "Torrkörning:  .venv/bin/python ptt_bridge.py --model $MODEL --dry-run"
