"""Shared Swedish/English UI catalogue for the menu and native app launcher."""
import os

STRINGS = {
    "Ingen telefon": ("Ingen telefon", "No phone connected"),
    "Ljudkälla": ("Ljudkälla", "Audio source"),
    "Transkribering": ("Transkribering", "Transcription"),
    "Whisper": ("Whisper", "Whisper"),
    "Pianissimo (sv)": ("Pianissimo (sv)", "Pianissimo (sv)"),
    "Pico-mick": ("Pico-mick", "Pico mic"),
    "Bordstelefon": ("Bordstelefon", "Desk phone"),
    "Lyssnar på telefonen": ("Lyssnar på telefonen", "Listening to the phone"),
    "Senaste: ": ("Senaste: ", "Latest: "),
    "Pausa": ("Pausa", "Pause"),
    "Fortsätt": ("Fortsätt", "Resume"),
    "Pausad": ("Pausad", "Paused"),
    "Lyssnar": ("Lyssnar", "Listening"),
    "Redo": ("Redo", "Ready"),
    "Hjälpmedelsbehörighet saknas": ("Hjälpmedelsbehörighet saknas", "Accessibility permission required"),
    "Startar…": ("Startar…", "Starting…"),
    "Startar bryggan…": ("Startar bryggan…", "Connecting to phone…"),
    "Transkriberingsspråk": ("Transkriberingsspråk", "Transcription language"),
    "language_pending": ("Transkriberingsspråk · byts efter påläggning", "Transcription language · changes after hang-up"),
    "Starta vid inloggning": ("Starta vid inloggning", "Launch at login"),
    "Behörigheter och hjälp…": ("Behörigheter och hjälp…", "Permissions and help…"),
    "Öppna logg": ("Öppna logg", "Open log"),
    "Avsluta Phone Prompter": ("Avsluta Phone Prompter", "Quit Phone Prompter"),
    "Öppna Hjälpmedel": ("Öppna Hjälpmedel", "Open Accessibility"),
    "Öppna Mikrofon": ("Öppna Mikrofon", "Open Microphone"),
    "Stäng": ("Stäng", "Close"),
    "help_body": (
        "Anslut USB-telefonen. Lyft luren och tala; lägg på för att skicka Enter.\n\n"
        "Tillåt Mikrofon och Hjälpmedel i Systeminställningar → Integritet och säkerhet. "
        "Appen behöver mikrofonen för att lyssna och Hjälpmedel för att skriva i det aktiva fältet.\n\n"
        "Välj Phone Prompter när du startat via appen; välj Terminal/Python om du kör skriptet direkt. "
        "Starta om Phone Prompter efter ändrade behörigheter.\n\n"
        "Menyerna följer macOS språk. Transkriberingsspråket väljer du separat i menyn. "
        "All transkribering körs lokalt på din Mac.",
        "Connect your USB phone. Pick up and speak; hang up to send Enter.\n\n"
        "Allow Microphone and Accessibility in System Settings → Privacy & Security. "
        "The app needs the microphone to listen and Accessibility to type into the active field.\n\n"
        "Select Phone Prompter when using the app, or Terminal/Python when running the script directly. "
        "Restart Phone Prompter after changing permissions.\n\n"
        "Menus follow your macOS language. Choose the transcription language separately in the menu. "
        "All transcription runs locally on your Mac."),
    "missing_file": ("En fil saknas: %@\nBygg om appen om du har flyttat projektet.",
                     "A file is missing: %@\nRebuild the app if you have moved the project."),
    "process_failed": ("Phone Prompter kunde inte fortsätta. Kontrollera loggen för detaljer.",
                       "Phone Prompter could not continue. Check the log for details."),
    "NSMicrophoneUsageDescription": (
        "Phone Prompter använder USB-telefonens mikrofon för att skriva det du säger, lokalt på din Mac.",
        "Phone Prompter uses your USB phone's microphone to type what you say, locally on your Mac."),
}


def choose_language(preferences):
    for language in preferences:
        base = language.replace("_", "-").split("-")[0].lower()
        if base in ("sv", "en"):
            return base
    return "en"


def system_language():
    # The native launcher resolves macOS's per-app preference, then passes it on.
    override = os.environ.get("PHONE_PROMPTER_UI_LANGUAGE")
    if override in ("sv", "en"):
        return override
    try:
        from Foundation import NSLocale
        return choose_language(NSLocale.preferredLanguages())
    except ImportError:
        return "en"


def translate(key, language):
    return STRINGS[key][0 if language == "sv" else 1]
