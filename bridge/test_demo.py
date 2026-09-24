"""Tester för demoskärmen. Körs utan webbläsare eller ljud:

    cd bridge && python3 -m unittest test_demo.py
"""
import json
import queue
import unittest

import demo
import ptt_bridge as pb


class Fangare:
    """Låtsas-typer som minns vad som skrevs."""
    def __init__(self):
        self.skrivet = []
    def type(self, text):
        self.skrivet.append(text)


class DemoTyperTest(unittest.TestCase):
    def setUp(self):
        self.skarm = demo.Demoskarm(port=0, log=lambda *a: None)
        self.k = queue.Queue(maxsize=16)
        self.skarm.koer.append(self.k)
        self.inre = Fangare()
        self.typer = demo.DemoTyper(self.inre, self.skarm)

    def test_texten_gar_vidare_till_tangentbordet(self):
        # Demot får inte äta upp utskriften — det är fortfarande poängen.
        self.typer.type("hej på dig ")
        self.assertEqual(self.inre.skrivet, ["hej på dig "])

    def test_texten_visas_ocksa_pa_skarmen(self):
        self.typer.type("hej på dig ")
        rad = self.k.get_nowait()
        self.assertIn("event: slutlig", rad)
        self.assertEqual(json.loads(rad.split("data: ")[1])["text"], "hej på dig")

    def test_okanda_attribut_gar_till_den_inre(self):
        self.inre.nagot = 42
        self.assertEqual(self.typer.nagot, 42)


class HandelseformatTest(unittest.TestCase):
    """Formatet måste vara giltig server-sent-events."""

    def setUp(self):
        self.skarm = demo.Demoskarm(port=0, log=lambda *a: None)
        self.k = queue.Queue(maxsize=16)
        self.skarm.koer.append(self.k)

    def test_preliminar_har_ratt_form(self):
        self.skarm.preliminar("halv mening")
        rad = self.k.get_nowait()
        self.assertTrue(rad.startswith("event: preliminar\ndata: "))
        self.assertTrue(rad.endswith("\n\n"))

    def test_svenska_tecken_overlever(self):
        self.skarm.slutlig("Gösta åt räksmörgås")
        data = json.loads(self.k.get_nowait().split("data: ")[1])
        self.assertEqual(data["text"], "Gösta åt räksmörgås")

    def test_lage_minns_kallan(self):
        self.skarm.lage(True, kalla="Pico-mick")
        self.k.get_nowait()
        self.skarm.lage(False)          # utan källa — ska behållas
        data = json.loads(self.k.get_nowait().split("data: ")[1])
        self.assertEqual(data["kalla"], "Pico-mick")
        self.assertFalse(data["lyssnar"])

    def test_full_ko_tappar_hellre_an_blockerar(self):
        # En flik som hängt sig får inte stoppa bryggan.
        liten = queue.Queue(maxsize=1)
        self.skarm.koer = [liten]
        for _ in range(5):
            self.skarm.slutlig("x")     # ska inte kasta
        self.assertEqual(liten.qsize(), 1)


class PagaendeTest(unittest.TestCase):
    """Preliminär text kräver att man kan läsa frasen som pågår."""

    def test_tomt_innan_nagon_talat(self):
        seg = pb.Segmenter(rate=8000)
        self.assertEqual(seg.pagaende(), b"")

    def test_ger_det_som_samlats(self):
        import array
        rate = 8000
        seg = pb.Segmenter(rate=rate, min_silence_ms=350, max_segment_ms=8000)
        blk = array.array("h", [900, -900] * ((rate * 10 // 1000) * 50 // 2)).tobytes()
        seg.push(blk)
        self.assertGreater(len(seg.pagaende()), 0)

    def test_rors_inte_av_att_man_laser(self):
        import array
        rate = 8000
        seg = pb.Segmenter(rate=rate)
        blk = array.array("h", [900, -900] * ((rate * 10 // 1000) * 50 // 2)).tobytes()
        seg.push(blk)
        self.assertEqual(seg.pagaende(), seg.pagaende())


class FlaggaTest(unittest.TestCase):
    def test_demo_ar_av_som_standard(self):
        self.assertFalse(pb.bygg_argparser().parse_args([]).demo)

    def test_demo_har_egen_port(self):
        a = pb.bygg_argparser().parse_args(["--demo"])
        self.assertTrue(a.demo)
        self.assertNotIn(a.demo_port, (8178, 8179, 8180))


class AteruppspelningTest(unittest.TestCase):
    """En flik som öppnas mitt i en mening ska se den, inte tom skärm.

    SSE levererar bara det som sänds medan man är ansluten, så skärmen
    måste minnas sitt läge och spela upp det för nya anslutningar.
    """

    def setUp(self):
        self.skarm = demo.Demoskarm(port=0, log=lambda *a: None)

    def test_minns_senaste_preliminara(self):
        self.skarm.preliminar("halv mening")
        self.assertEqual(self.skarm.senaste_text, ("preliminar", "halv mening"))

    def test_slutlig_ersatter_preliminar(self):
        self.skarm.preliminar("halv")
        self.skarm.slutlig("hel mening.")
        self.assertEqual(self.skarm.senaste_text, ("slutlig", "hel mening."))

    def test_pålagt_nollstaller(self):
        # När luren läggs på finns ingen pågående fras att spela upp.
        self.skarm.preliminar("halv")
        self.skarm.lage(False)
        self.assertIsNone(self.skarm.senaste_text)

    def test_lyft_nollstaller_ocksa(self):
        self.skarm.slutlig("förra frasen")
        self.skarm.lage(True)
        self.assertIsNone(self.skarm.senaste_text)


class MenyradsDemoTest(unittest.TestCase):
    """Demot måste gå att slå på där bryggan faktiskt startas.

    Menyradsappen startar ptt_bridge.py; utan ett val där kan demot bara
    nås genom att köra bryggan för hand, och då slåss två bryggor om
    mikrofonen.
    """

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

    def test_av_som_standard(self):
        self.assertFalse(self.mb.load_demo())

    def test_gar_att_sla_pa(self):
        self.mb.save_demo(True)
        self.assertTrue(self.mb.load_demo())
        self.assertIn("--demo", self.mb.bryggans_argument("pico", "sv"))

    def test_flaggan_uteblir_nar_av(self):
        self.assertNotIn("--demo", self.mb.bryggans_argument("pico", "sv"))

    def test_raderar_inte_ovriga_installningar(self):
        self.mb.save_language("en")
        self.mb.save_kalla("telefon")
        self.mb.save_demo(True)
        self.assertEqual(self.mb.load_language(), "en")
        self.assertEqual(self.mb.load_kalla(), "telefon")


class PreliminarTradTest(unittest.TestCase):
    """Preliminär transkribering får aldrig blockera huvudloopen.

    poll() måste fortsätta tömma inspelaren medan modellen arbetar, annars
    tappas ljud och hela bryggan känns trög.
    """

    def bygg(self, fordrojning=0.4):
        import time
        import ptt_bridge as pb

        class Långsam:
            def __init__(s): s.anrop = 0
            def transcribe(s, wav):
                s.anrop += 1
                time.sleep(fordrojning)
                return "halv mening"

        class Inspelare:
            rate = 8000
            def take(s): return b""
            def start(s): pass
            def stop(s): pass

        class Skärm:
            def __init__(s): s.texter = []
            def preliminar(s, t): s.texter.append(t)
            def slutlig(s, t): pass
            def lage(s, *a, **k): pass

        class Länk:
            def send(s, rad): pass

        modell, skarm = Långsam(), Skärm()
        b = pb.Bridge(link=Länk(), recorder=Inspelare(), transcriber=modell,
                      typer=type("T", (), {"type": lambda s, t: None})(),
                      log=lambda *a: None, sounds=False, demo=skarm)
        # Lägg ljud i den pågående frasen: en sekund räcker över tröskeln
        b.seg.seg = [b"\x30\x30" * 400 for _ in range(30)]
        return b, modell, skarm

    def test_returnerar_innan_modellen_ar_klar(self):
        import time
        b, modell, skarm = self.bygg()
        t0 = time.monotonic()
        b._demo_preliminar()
        gick = time.monotonic() - t0
        self.assertLess(gick, 0.15, f"blockerade {gick:.2f} s på transkriberingen")

    def test_texten_nar_skarmen_nar_modellen_blir_klar(self):
        import time
        b, modell, skarm = self.bygg(fordrojning=0.1)
        b._demo_preliminar()
        time.sleep(0.5)
        self.assertEqual(modell.anrop, 1)
        self.assertEqual(skarm.texter, ["halv mening"])

    def test_inget_nytt_anrop_medan_ett_pagar(self):
        # Annars köar sig anropen och modellen blir flaskhals.
        import time
        b, modell, skarm = self.bygg(fordrojning=0.4)
        b._demo_preliminar()
        b.demo_nasta = 0.0            # tiden är inte hindret vi provar
        b._demo_preliminar()
        time.sleep(0.7)
        self.assertEqual(modell.anrop, 1, "startade ett andra anrop för tidigt")
