"""Tester för menyradsappens beslutslogik (ingen GUI)."""
import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile
import plistlib

import menubar as mb
from ui_strings import choose_language, system_language, translate


class LocalizationTest(unittest.TestCase):
    def test_respects_preference_order_and_regional_variants(self):
        self.assertEqual(choose_language(['de-DE', 'sv-SE', 'en-US']), 'sv')
        self.assertEqual(choose_language(['en_GB', 'sv']), 'en')
        self.assertEqual(choose_language(['fr-FR']), 'en')
        self.assertEqual(choose_language([]), 'en')

    def test_app_ui_language_does_not_change_transcription_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(mb, 'SETTINGS', str(Path(tmp) / 'settings.json')):
                mb.save_language('sv')
                with patch.dict('os.environ', {'PHONE_PROMPTER_UI_LANGUAGE': 'en'}):
                    self.assertEqual(system_language(), 'en')
                    self.assertEqual(translate('Lyssnar', system_language()), 'Listening')
                    self.assertEqual(mb.load_language(), 'sv')
                with patch.dict('os.environ', {'PHONE_PROMPTER_UI_LANGUAGE': 'sv'}):
                    self.assertEqual(translate('Lyssnar', system_language()), 'Lyssnar')


class SupervisorTest(unittest.TestCase):
    def test_startar_nar_telefonen_kopplas_in(self):
        s = mb.Supervisor()
        self.assertEqual(s.decide(present=True, running=False), "start")

    def test_stoppar_nar_telefonen_dras_ur(self):
        s = mb.Supervisor()
        self.assertEqual(s.decide(present=False, running=True), "stop")

    def test_gor_inget_i_stabilt_lage(self):
        s = mb.Supervisor()
        self.assertIsNone(s.decide(present=True, running=True))
        self.assertIsNone(s.decide(present=False, running=False))

    def test_pausad_startar_inte_och_stoppar(self):
        s = mb.Supervisor()
        s.paused = True
        self.assertIsNone(s.decide(present=True, running=False))
        self.assertEqual(s.decide(present=True, running=True), "stop")

    def test_krasch_backar_av(self):
        s = mb.Supervisor(now=lambda: 100.0)
        s.note_exit(code=1)
        self.assertIsNone(s.decide(present=True, running=False))   # inom backoff
        s.now = lambda: 100.0 + s.backoff + 1
        self.assertEqual(s.decide(present=True, running=False), "start")

    def test_normal_avslutning_ingen_backoff(self):
        s = mb.Supervisor(now=lambda: 100.0)
        s.note_exit(code=0)
        self.assertEqual(s.decide(present=True, running=False), "start")


class ParseTest(unittest.TestCase):
    def test_tolkar_bryggans_logg(self):
        self.assertEqual(mb.parse_log_line("11:20:22 lyft: lyssnar"), ("state", "Lyssnar"))
        self.assertEqual(mb.parse_log_line("11:20:30 pålagt"), ("state", "Redo"))
        self.assertEqual(mb.parse_log_line("11:20:25 2.1 s → 0.3 s: 'Hej du'  [tal 1200 ms]"), ("text", "Hej du"))
        self.assertIsNone(mb.parse_log_line("11:20:22 whisper-server igång"))


class PackagingTest(unittest.TestCase):
    def test_login_starts_installed_app_and_escapes_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'launch.plist'
            with patch.object(mb, 'PLIST', str(path)), patch.object(mb, 'APP_BUNDLE', '/Apps/A & B/Phone Prompter.app'):
                mb.write_plist()
            value = plistlib.loads(path.read_bytes())
            self.assertEqual(value['ProgramArguments'], ['/usr/bin/open', '-g', '/Apps/A & B/Phone Prompter.app'])
            self.assertFalse(value['KeepAlive'])

    def test_language_is_saved_and_invalid_settings_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            with patch.object(mb, 'SETTINGS', str(path)):
                self.assertEqual(mb.load_language(), 'sv')
                mb.save_language('en')
                self.assertEqual(mb.load_language(), 'en')
                path.write_text('{broken')
                self.assertEqual(mb.load_language(), 'sv')
                with self.assertRaises(ValueError):
                    mb.save_language('other')


if __name__ == "__main__":
    unittest.main()


class KallaTest(unittest.TestCase):
    """Ljudkällan: Pico-micken eller bordstelefonen via multicast."""

    def setUp(self):
        self.kat = tempfile.TemporaryDirectory()
        self.fil = str(Path(self.kat.name) / "settings.json")
        self.p = patch.object(mb, "SETTINGS", self.fil)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.kat.cleanup()

    def test_standard_ar_pico(self):
        # Befintliga användare får inte byta beteende av en uppgradering.
        self.assertEqual(mb.load_kalla(), "pico")

    def test_sparar_och_laser_tillbaka(self):
        mb.save_kalla("telefon")
        self.assertEqual(mb.load_kalla(), "telefon")

    def test_okand_kalla_faller_tillbaka(self):
        mb.save_kalla("telefon")
        Path(self.fil).write_text('{"kalla": "hittepa"}')
        self.assertEqual(mb.load_kalla(), "pico")

    def test_kalla_raderar_inte_spraket(self):
        # save_language skrev hela filen och hade annars slagit ut källan.
        mb.save_language("en")
        mb.save_kalla("telefon")
        self.assertEqual(mb.load_language(), "en")
        self.assertEqual(mb.load_kalla(), "telefon")

    def test_sprak_raderar_inte_kallan(self):
        mb.save_kalla("telefon")
        mb.save_language("en")
        self.assertEqual(mb.load_kalla(), "telefon")
        self.assertEqual(mb.load_language(), "en")

    def test_ogiltig_kalla_avvisas(self):
        with self.assertRaises(ValueError):
            mb.save_kalla("fax")


class NarvaroTest(unittest.TestCase):
    """Telefonen finns alltid på nätet; Pico:n måste vara inkopplad."""

    def test_telefonen_ar_alltid_narvarande(self):
        self.assertTrue(mb.kalla_narvarande("telefon"))

    def test_pico_kraver_usb(self):
        with patch.object(mb, "phone_present", return_value=False):
            self.assertFalse(mb.kalla_narvarande("pico"))
        with patch.object(mb, "phone_present", return_value=True):
            self.assertTrue(mb.kalla_narvarande("pico"))


class StartArgumentTest(unittest.TestCase):
    """--multicast ska bara läggas på för telefon-källan."""

    def test_telefon_ger_multicast(self):
        self.assertIn("--multicast", mb.bryggans_argument("telefon", "sv"))

    def test_pico_ger_inte_multicast(self):
        self.assertNotIn("--multicast", mb.bryggans_argument("pico", "sv"))

    def test_spraket_foljer_med_i_bada(self):
        for kalla in ("pico", "telefon"):
            args = mb.bryggans_argument(kalla, "en")
            self.assertIn("--language", args)
            self.assertIn("en", args)


class OversattningTest(unittest.TestCase):
    """Varje tr()-nyckel måste finnas i tabellen.

    translate() kastar KeyError på okänd nyckel, och menybygget körs aldrig i
    testerna — en ny text utan översättning kraschade därför först i skarp
    drift, efter att 86 tester passerat.
    """

    def test_alla_nycklar_finns(self):
        import re
        from ui_strings import STRINGS
        kalla = Path(__file__).with_name("menubar.py").read_text()
        nycklar = set(re.findall(r'tr\("([^"]+)"\)', kalla))
        saknas = sorted(n for n in nycklar if n not in STRINGS)
        self.assertEqual(saknas, [], f"saknar översättning: {saknas}")

    def test_statustexter_finns(self):
        # set_status() skickar sina texter genom tr().
        from ui_strings import STRINGS
        for text in ("Ingen telefon", "Pausad", "Startar…", "Redo",
                     "Lyssnar på telefonen", "Startar bryggan…"):
            self.assertIn(text, STRINGS)
