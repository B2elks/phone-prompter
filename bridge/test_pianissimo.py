"""Tester för Pianissimo-skalet. Körs med vanlig python, utan NeMo:

    cd bridge && python3 -m unittest test_pianissimo.py
"""
import io
import unittest
import wave

import pianissimo_server as ps
import ptt_bridge as pb


def wav_bytes(rate, sekunder=0.1, kanaler=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(kanaler)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * int(rate * sekunder) * kanaler)
    return buf.getvalue()


class MultipartTest(unittest.TestCase):
    """Skalet måste tolka exakt det bryggan skickar."""

    def test_plockar_ut_wav_som_bryggan_skickar(self):
        data = wav_bytes(8000)
        ctype, body = pb.multipart({"response_format": "json", "language": "sv"},
                                   "file", "clip.wav", data)
        self.assertEqual(ps.extrahera_wav(body, ctype), data)

    def test_binart_innehall_overlever(self):
        # Ljuddata innehåller alla byte-värden; en textbaserad tolkare
        # skulle förstöra det.
        data = bytes(range(256)) * 4
        ctype, body = pb.multipart({}, "file", "clip.wav", data)
        self.assertEqual(ps.extrahera_wav(body, ctype), data)

    def test_utan_filfalt_ger_none(self):
        ctype, body = pb.multipart({"language": "sv"}, "annat", "x.txt", b"hej")
        self.assertIsNone(ps.extrahera_wav(body, ctype))

    def test_trasig_content_type_ger_none(self):
        self.assertIsNone(ps.extrahera_wav(b"skrap", "text/plain"))


class OmsamplingTest(unittest.TestCase):
    """Modellen vill ha 16 kHz mono; källorna är 8 kHz och 48 kHz."""

    def test_8k_blir_16k(self):
        prov, rate = ps.wav_till_prov(wav_bytes(8000, 0.5))
        self.assertEqual(rate, 16000)
        self.assertAlmostEqual(len(prov) / 16000, 0.5, places=1)

    def test_48k_blir_16k(self):
        prov, rate = ps.wav_till_prov(wav_bytes(48000, 0.5))
        self.assertEqual(rate, 16000)
        self.assertAlmostEqual(len(prov) / 16000, 0.5, places=1)

    def test_16k_lamnas_ifred(self):
        prov, rate = ps.wav_till_prov(wav_bytes(16000, 0.5))
        self.assertEqual(rate, 16000)
        self.assertAlmostEqual(len(prov) / 16000, 0.5, places=1)

    def test_stereo_blir_mono(self):
        prov, rate = ps.wav_till_prov(wav_bytes(16000, 0.5, kanaler=2))
        self.assertEqual(prov.ndim, 1)

    def test_prov_ligger_inom_minus_ett_och_ett(self):
        # NeMo vill ha flyttal normaliserade, inte heltal.
        prov, _ = ps.wav_till_prov(wav_bytes(8000, 0.2))
        self.assertTrue((prov >= -1.0).all() and (prov <= 1.0).all())


class SvarTest(unittest.TestCase):
    def test_svaret_har_samma_form_som_whisper_server(self):
        # Bryggan läser json["text"] — inget annat.
        kropp = ps.json_svar("hej på dig")
        self.assertEqual(pb.parse_inference_json(kropp), "hej på dig")

    def test_tom_transkribering_ger_tom_strang(self):
        self.assertEqual(pb.parse_inference_json(ps.json_svar("")), "")


class MotorvalTest(unittest.TestCase):
    """Vilken transkriberare bryggan väljer, utan att starta något."""

    def valj(self, argv):
        args = pb.bygg_argparser().parse_args(argv)
        args.model, args.port = pb.recognition_defaults(args.language)
        return pb.valj_transkriberare(args, log=lambda *a: None)

    def test_standard_ar_whisper(self):
        # Pico-vägen i daglig drift får inte ändras av att ett val tillkommer.
        t = self.valj([])
        self.assertIsInstance(t, pb.WhisperServer)

    def test_pianissimo_valjs_med_flagga(self):
        t = self.valj(["--engine", "pianissimo"])
        self.assertIsInstance(t, pb.PianissimoServer)

    def test_pianissimo_har_egen_port(self):
        # Får inte krocka med whisper på 8178 (sv) eller 8180 (en).
        t = self.valj(["--engine", "pianissimo"])
        self.assertNotIn(t.port, (8178, 8180))

    def test_pianissimo_avvisar_engelska(self):
        # Modellen kan bara svenska; tyst fel vore värre än ett avbrott.
        with self.assertRaises(SystemExit):
            self.valj(["--engine", "pianissimo", "--language", "en"])

    def test_samma_granssnitt_som_whisper(self):
        for namn in ("transcribe", "__enter__", "__exit__"):
            self.assertTrue(hasattr(pb.PianissimoServer, namn), f"saknar {namn}")


class MenyradsMotorTest(unittest.TestCase):
    """Motorvalet i menyraden. Pianissimo kan bara svenska."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import menubar as mb
        self.mb = mb
        self.kat = tempfile.TemporaryDirectory()
        self.p = patch.object(mb, "SETTINGS", str(Path(self.kat.name) / "s.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.kat.cleanup()

    def test_standard_ar_whisper(self):
        self.assertEqual(self.mb.load_motor(), "whisper")

    def test_sparas_och_lases_tillbaka(self):
        self.mb.save_motor("pianissimo")
        self.assertEqual(self.mb.load_motor(), "pianissimo")

    def test_okant_varde_faller_tillbaka(self):
        from pathlib import Path
        Path(self.mb.SETTINGS).write_text('{"motor": "hittepa"}')
        self.assertEqual(self.mb.load_motor(), "whisper")

    def test_pianissimo_ger_flaggan_pa_svenska(self):
        self.mb.save_motor("pianissimo")
        self.assertIn("--engine", self.mb.bryggans_argument("pico", "sv"))

    def test_pianissimo_hoppas_over_pa_engelska(self):
        # Modellen kan inte engelska. Tyst fallback till whisper är bättre
        # än att bryggan avslutar och startas om i en loop av övervakaren.
        self.mb.save_motor("pianissimo")
        self.assertNotIn("--engine", self.mb.bryggans_argument("pico", "en"))

    def test_motor_raderar_inte_ovriga_installningar(self):
        self.mb.save_language("en")
        self.mb.save_kalla("telefon")
        self.mb.save_motor("pianissimo")
        self.assertEqual(self.mb.load_language(), "en")
        self.assertEqual(self.mb.load_kalla(), "telefon")
