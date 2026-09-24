#!/usr/bin/env python3
"""Menyradsapp (☎) som startar bryggan när telefonen kopplas in och stoppar den
när den dras ur. Visar status och senaste fras, kan pausas, kan starta vid
inloggning.

  .venv/bin/python menubar.py
  ./install_menubar.sh     # LaunchAgent: startar appen vid inloggning
"""
import os
import fcntl
import json
import plistlib
import queue
import re
import signal
import subprocess
import sys
import threading
import time

from ui_strings import system_language, translate

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.expanduser("~/Library/Logs/phone-prompter.log")
PLIST = os.path.expanduser("~/Library/LaunchAgents/se.skyttberg.phone-prompter.plist")
USB_VID, USB_PID = 0xCAFE, 0x4011
APP_BUNDLE = os.environ.get("PHONE_PROMPTER_APP_BUNDLE")
SETTINGS = os.path.expanduser("~/Library/Application Support/Phone Prompter/settings.json")


KALLOR = ("pico", "telefon")
MOTORER = ("whisper", "pianissimo")


GAMLA_SETTINGS = os.path.expanduser("~/Library/Application Support/Pico PTT/settings.json")


def _las_installningar():
    # Hette Pico PTT tidigare; läs den gamla filen hellre än att tappa valen.
    kalla = SETTINGS if os.path.exists(SETTINGS) else GAMLA_SETTINGS
    try:
        with open(kalla) as f:
            varden = json.load(f)
        return varden if isinstance(varden, dict) else {}
    except (OSError, ValueError):
        return {}


def _skriv_installning(nyckel, varde):
    """Läs-ändra-skriv, så en inställning inte raderar en annan.

    Tidigare skrevs hela filen som {"language": ...}; att spara språket hade
    då slagit ut ljudkällan utan att det syntes.
    """
    varden = _las_installningar()
    varden[nyckel] = varde
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS + ".tmp", "w") as f:
        json.dump(varden, f)
    os.replace(SETTINGS + ".tmp", SETTINGS)


def load_language():
    value = _las_installningar().get("language", "sv")
    return value if value in ("sv", "en") else "sv"


def save_language(language):
    if language not in ("sv", "en"):
        raise ValueError("Okänt språk")
    _skriv_installning("language", language)


def load_motor():
    """Transkriberare: "whisper" eller "pianissimo".

    Standard är whisper, så en uppgradering inte ändrar beteendet — och
    inte heller drar igång en modell som tar 5,5 GB minne utan att någon
    bett om det.
    """
    varde = _las_installningar().get("motor", "whisper")
    return varde if varde in MOTORER else "whisper"


def save_motor(motor):
    if motor not in MOTORER:
        raise ValueError("Okänd transkriberare")
    _skriv_installning("motor", motor)


def load_kalla():
    """Ljudkälla: "pico" (USB-micken) eller "telefon" (multicast-paging).

    Standard är pico, så en uppgradering inte ändrar beteendet för den som
    använder micken.
    """
    varde = _las_installningar().get("kalla", "pico")
    return varde if varde in KALLOR else "pico"


def save_kalla(kalla):
    if kalla not in KALLOR:
        raise ValueError("Okänd ljudkälla")
    _skriv_installning("kalla", kalla)


# ------------------------------------------------------------- ren logik

class Supervisor:
    """Bestämmer start/stop av bryggan utifrån om telefonen finns, om bryggan
    kör och om användaren pausat. Kraschar bryggan väntar vi backoff innan
    omstart så en trasig installation inte snurrar."""

    def __init__(self, now=time.monotonic, backoff=5.0):
        self.now = now
        self.backoff = backoff
        self.paused = False
        self.not_before = 0.0

    def decide(self, present, running):
        if running and (not present or self.paused):
            return "stop"
        if present and not running and not self.paused and self.now() >= self.not_before:
            return "start"
        return None

    def note_exit(self, code):
        if code != 0:
            self.not_before = self.now() + self.backoff


def parse_log_line(line):
    body = re.sub(r"^\d\d:\d\d:\d\d ", "", line.strip())
    if body.startswith("lyft:"):
        return ("state", "Lyssnar")
    if body == "pålagt":
        return ("state", "Redo")
    m = re.match(r".*→ [\d.]+ s: '(.*)'", body)
    if m:
        return ("text", m.group(1))
    return None


def phone_present():
    try:
        from serial.tools import list_ports
        return any(p.vid == USB_VID and p.pid == USB_PID for p in list_ports.comports())
    except Exception:
        return False


def kalla_narvarande(kalla):
    """Finns källan att lyssna på?

    Bordstelefonen sitter alltid på nätet och bryggan väntar bara på paket,
    vilket inte kostar något när ingen talar — därför alltid närvarande.
    Pico:n måste däremot vara inkopplad på USB.
    """
    if kalla == "telefon":
        return True
    return phone_present()


def bryggans_argument(kalla, language):
    args = ["--language", language]
    if kalla == "telefon":
        args.append("--multicast")
    # Pianissimo kan bara svenska. Att ändå skicka flaggan skulle få bryggan
    # att avsluta direkt, och övervakaren startar om den i en loop.
    if load_motor() == "pianissimo" and language == "sv":
        args += ["--engine", "pianissimo"]
    return args


# ------------------------------------------------------------- GUI

def main():
    import rumps
    from Quartz import CGPreflightPostEventAccess, CGRequestPostEventAccess
    from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

    ui_language = system_language()
    tr = lambda key: translate(key, ui_language)

    class App(rumps.App):
        def __init__(self):
            super().__init__("Phone Prompter", title="☎", quit_button=None)
            self.sup = Supervisor()
            self.proc = None
            self.events = queue.Queue()
            self.next_scan = 0
            self.language = load_language()
            self.kalla = load_kalla()
            self.motor = load_motor()
            self.in_call = False
            self.restart_pending = False
            self.language_menu = rumps.MenuItem(tr("Transkriberingsspråk"))
            self.language_items = {}
            for code, name in (("sv", "Svenska"), ("en", "English")):
                item = rumps.MenuItem(name, callback=lambda sender, c=code: self.change_language(c))
                item.state = code == self.language
                self.language_items[code] = item
                self.language_menu.add(item)
            self.kalla_menu = rumps.MenuItem(tr("Ljudkälla"))
            self.kalla_items = {}
            for kod, namn in (("pico", tr("Pico-mick")), ("telefon", tr("Bordstelefon"))):
                item = rumps.MenuItem(namn, callback=lambda sender, k=kod: self.change_kalla(k))
                item.state = kod == self.kalla
                self.kalla_items[kod] = item
                self.kalla_menu.add(item)
            self.motor_menu = rumps.MenuItem(tr("Transkribering"))
            self.motor_items = {}
            for kod, namn in (("whisper", tr("Whisper")), ("pianissimo", tr("Pianissimo (sv)"))):
                item = rumps.MenuItem(namn, callback=lambda sender, m=kod: self.change_motor(m))
                item.state = kod == self.motor
                self.motor_items[kod] = item
                self.motor_menu.add(item)
            self.status_item = rumps.MenuItem(tr("Ingen telefon"))
            self.text_item = rumps.MenuItem(tr("Senaste: ") + "–")
            self.pause_item = rumps.MenuItem(tr("Pausa"), callback=self.toggle_pause)
            self.login_item = rumps.MenuItem(tr("Starta vid inloggning"), callback=self.toggle_login)
            self.login_item.state = os.path.exists(PLIST)
            self.menu = [self.status_item, self.text_item, None, self.pause_item, self.kalla_menu,
                         self.motor_menu, self.language_menu, self.login_item,
                         rumps.MenuItem(tr("Behörigheter och hjälp…"), callback=self.help),
                         rumps.MenuItem(tr("Öppna logg"), callback=lambda _: subprocess.run(["open", LOG])),
                         None, rumps.MenuItem(tr("Avsluta Phone Prompter"), callback=self.quit)]
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            self.timer = rumps.Timer(self.tick, 0.25)
            self.timer.start()
            signal.signal(signal.SIGTERM, lambda *_: self.quit(None))
            signal.signal(signal.SIGINT, lambda *_: self.quit(None))
            if not CGPreflightPostEventAccess():
                AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
                CGRequestPostEventAccess()

        def set_status(self, text, icon):
            self.current_status = (text, icon)
            if time.monotonic() >= getattr(self, "next_permission_check", 0):
                allowed = CGPreflightPostEventAccess()
                if allowed != getattr(self, "can_type", None):
                    print(time.strftime("%H:%M:%S"), "keyboard permission:",
                          "granted" if allowed else "denied", flush=True)
                self.can_type = allowed
                self.next_permission_check = time.monotonic() + 2
            if text in ("Redo", "Lyssnar", "Startar bryggan…") and not self.can_type:
                text, icon = "Hjälpmedelsbehörighet saknas", "⚠︎"
            self.status_item.title = tr(text)
            self.title = icon

        def tick(self, _=None):
            if hasattr(self, "current_status"):
                self.set_status(*self.current_status)
            # Cocoa-objekt uppdateras bara från huvudtråden.
            while not self.events.empty():
                proc, line = self.events.get_nowait()
                if proc is not self.proc:
                    continue
                ev = parse_log_line(line)
                if ev and ev[0] == "state":
                    self.in_call = ev[1] == "Lyssnar"
                    self.set_status(ev[1], "☎" if ev[1] == "Redo" else "🔴")
                elif ev and ev[0] == "text":
                    self.text_item.title = tr("Senaste: ") + (ev[1][:60] + ("…" if len(ev[1]) > 60 else ""))
                elif "klar:" in line:
                    self.set_status("Redo", "☎")
            if self.restart_pending and not self.in_call:
                self.stop()
                self.restart_pending = False
                self.language_menu.title = tr("Transkriberingsspråk")
                self.next_scan = 0
            if time.monotonic() < self.next_scan:
                return
            self.next_scan = time.monotonic() + 2
            running = self.proc is not None and self.proc.poll() is None
            if self.proc is not None and not running:
                self.sup.note_exit(self.proc.returncode)
                # Även egna Whisper-barn stängs; en redan delad server påverkas inte.
                terminate_group(self.proc)
                self.proc = None
                self.in_call = False
            present = kalla_narvarande(self.kalla)
            action = self.sup.decide(present, running)
            if action == "start":
                self.start()
            elif action == "stop":
                self.stop()
            elif not running:
                if self.sup.paused:
                    self.set_status("Pausad", "☏")
                elif self.kalla == "telefon":
                    self.set_status("Lyssnar på telefonen" if present else "Startar…", "☎")
                else:
                    self.set_status("Startar…" if present else "Ingen telefon",
                                    "☎" if present else "☏")

        def start(self):
            with open(LOG, "a") as log:
                self.proc = subprocess.Popen([sys.executable, os.path.join(HERE, "ptt_bridge.py")]
                                             + bryggans_argument(self.kalla, self.language),
                                             stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1,
                                             start_new_session=True)
            threading.Thread(target=self.pump, args=(self.proc,), daemon=True).start()
            self.set_status("Startar bryggan…", "☎")

        def pump(self, proc):
            with open(LOG, "a") as log:
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    self.events.put((proc, line))
            proc.stdout.close()

        def stop(self):
            if self.proc:
                terminate_group(self.proc)
            self.proc = None
            self.in_call = False
            self.set_status("Pausad" if self.sup.paused else "Ingen telefon", "☏")

        def toggle_pause(self, item):
            self.sup.paused = not self.sup.paused
            item.title = tr("Fortsätt" if self.sup.paused else "Pausa")
            self.next_scan = 0
            self.tick()

        def change_kalla(self, kalla):
            if kalla == self.kalla:
                return
            save_kalla(kalla)
            self.kalla = kalla
            for kod, item in self.kalla_items.items():
                item.state = kod == kalla
            # Bryggan måste startas om: flaggan sätts bara vid start.
            self.restart_pending = True

        def change_motor(self, motor):
            if motor == self.motor:
                return
            save_motor(motor)
            self.motor = motor
            for kod, item in self.motor_items.items():
                item.state = kod == motor
            self.restart_pending = True

        def change_language(self, language):
            if language == self.language:
                return
            save_language(language)
            self.language = language
            for code, item in self.language_items.items():
                item.state = code == language
            self.restart_pending = True
            self.language_menu.title = tr("Transkriberingsspråk")
            if self.in_call:
                self.language_menu.title = tr("language_pending")
            self.tick()

        def help(self, _):
            result = rumps.alert(title="Phone Prompter", message=tr("help_body"),
                                 ok=tr("Öppna Hjälpmedel"), cancel=tr("Stäng"), other=tr("Öppna Mikrofon"))
            pane = "Privacy_Accessibility" if result == 1 else "Privacy_Microphone" if result == -1 else None
            if pane:
                subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?" + pane])

        def toggle_login(self, item):
            # Ingen launchctl unload här: appen kör oftast själv under den
            # LaunchAgenten, och unload dödar då appen mitt i klicket.
            # Utan plist startar den bara inte nästa inloggning.
            if os.path.exists(PLIST):
                os.remove(PLIST)
                item.state = False
            else:
                write_plist()
                item.state = True

        def quit(self, _):
            self.stop()
            rumps.quit_application()

    # Hindra två menyradsappar från att ta samma serieport.
    support = os.path.expanduser("~/Library/Application Support/Phone Prompter")
    os.makedirs(support, exist_ok=True)
    with open(os.path.join(support, "running.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        App().run()


def terminate_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(2)


def write_plist():
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    arguments = (["/usr/bin/open", "-g", APP_BUNDLE] if APP_BUNDLE else
                 [sys.executable, os.path.join(HERE, "menubar.py")])
    with open(PLIST, "wb") as f:
        plistlib.dump({"Label": "se.skyttberg.phone-prompter", "ProgramArguments": arguments,
                      "RunAtLoad": True, "KeepAlive": False, "StandardErrorPath": LOG}, f)


if __name__ == "__main__":
    if "--write-plist" in sys.argv:
        write_plist()
        print("skrev", PLIST)
    else:
        main()
