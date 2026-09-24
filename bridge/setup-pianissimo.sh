#!/bin/bash
# Bygger NeMo-miljön för Pianissimo-sv (svensk transkribering utan
# hallucinationer ur tystnad). Ungefär 1,7 GB beroenden plus 2,3 GB modell.
#
# Egen venv därför att NeMo inte stöder Python 3.14 — den listar bara 3.10,
# och 3.11 fungerar. Bryggans vanliga .venv rörs inte.
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for kandidat in python3.11 python3.10; do
    if command -v "$kandidat" >/dev/null; then PY=$(command -v "$kandidat"); break; fi
done
[ -n "$PY" ] || { echo "hittar varken python3.11 eller python3.10 — NeMo stöder inte 3.12+"; exit 1; }
echo "använder $PY ($("$PY" --version))"

"$PY" -m venv .venv-nemo
.venv-nemo/bin/pip install -q --upgrade pip
echo "installerar nemo_toolkit[asr] — detta tar några minuter"
.venv-nemo/bin/pip install -q "nemo_toolkit[asr]"

echo "hämtar modellen (2,3 GB) och kontrollerar att den svarar"
.venv-nemo/bin/python - <<'PYEOF'
import warnings; warnings.filterwarnings("ignore")
import nemo.collections.asr as nemo_asr
nemo_asr.models.ASRModel.from_pretrained("KlangAI/pianissimo-sv")
print("modellen laddad")
PYEOF

echo
echo "Klart. Välj Transkribering → Pianissimo (sv) i menyraden,"
echo "eller kör bryggan med --engine pianissimo."
