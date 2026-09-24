#!/bin/bash
# Installerar menyradsappen som LaunchAgent (startar vid inloggning) och startar den nu.
set -euo pipefail
cd "$(dirname "$0")"

# Hette Pico PTT tidigare. Lämnas den gamla agenten kvar kör två instanser
# parallellt och båda knappar in samma text.
GAMMAL=~/Library/LaunchAgents/se.skyttberg.pico-ptt.plist
if [ -f "$GAMMAL" ]; then
    echo "tar bort den gamla Pico PTT-agenten"
    launchctl unload "$GAMMAL" 2>/dev/null || true
    rm -f "$GAMMAL"
    pkill -f "PicoPTT" 2>/dev/null || true
fi
APP="$HOME/Applications/Phone Prompter.app"
if [ -x "$APP/Contents/MacOS/PicoPTT" ]; then
    PHONE_PROMPTER_APP_BUNDLE="$APP" .venv/bin/python menubar.py --write-plist
    # Starta om en körande instans. "open" tar annars bara fram den gamla,
    # som har menubar.py inläst sedan den startade — en uppgradering av
    # koden får då ingen effekt förrän nästa inloggning.
    pkill -f "bridge/menubar.py" 2>/dev/null || true
    pkill -f "PicoPTT" 2>/dev/null || true
    sleep 1
    open "$APP"
    echo "Phone Prompter.app startad och inställd för start vid inloggning."
    exit 0
fi
.venv/bin/python -c "import rumps" 2>/dev/null || .venv/bin/pip install -q rumps
.venv/bin/python menubar.py --write-plist
pkill -f "bridge/menubar.py" 2>/dev/null || true
launchctl unload ~/Library/LaunchAgents/se.skyttberg.phone-prompter.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/se.skyttberg.phone-prompter.plist
echo "☎ i menyraden. Avinstallera: launchctl unload ~/Library/LaunchAgents/se.skyttberg.phone-prompter.plist && rm samma fil"
