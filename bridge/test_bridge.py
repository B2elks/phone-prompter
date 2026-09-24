"""Tester för bryggan. Körs utan Pico, ljudkort eller whisper: cd bridge && python3 -m unittest test_bridge.py"""
import io
import struct
import unittest
import wave
from unittest.mock import patch, MagicMock

import ptt_bridge as pb


class RecorderLifecycleTest(unittest.TestCase):
    def test_opens_only_on_pickup_and_closes_on_hangup(self):
        sd = MagicMock()
        sd.query_devices.return_value = [{'name': pb.MIC_NAME, 'max_input_channels': 1}]
        with patch.dict('sys.modules', {'sounddevice': sd}):
            recorder = pb.SoundDeviceRecorder()
        sd.InputStream.assert_not_called()
        recorder.start()
        stream = sd.InputStream.return_value
        stream.start.assert_called_once()
        recorder._cb(b'\x01\x00', 1, None, None)
        recorder.stop()
        stream.stop.assert_called_once()
        stream.close.assert_called_once()
        self.assertEqual(recorder.take(), b'\x01\x00')
        recorder._cb(b'\x02\x00', 1, None, None)
        self.assertEqual(recorder.take(), b'')
        recorder.stop()
        stream.close.assert_called_once()
        recorder.start()
        self.assertEqual(sd.InputStream.call_count, 2)

    def test_failed_start_releases_microphone(self):
        sd = MagicMock()
        sd.query_devices.return_value = [{'name': pb.MIC_NAME, 'max_input_channels': 1}]
        sd.InputStream.return_value.start.side_effect = RuntimeError('unavailable')
        with patch.dict('sys.modules', {'sounddevice': sd}):
            recorder = pb.SoundDeviceRecorder()
        with self.assertRaises(RuntimeError):
            recorder.start()
        sd.InputStream.return_value.close.assert_called_once()
        self.assertFalse(recorder.capturing)
        self.assertIsNone(recorder.stream)


class DictationCleaningTest(unittest.TestCase):
    def test_known_credit_sentences_are_removed(self):
        for text in ['britta.sdmedia.se', 'www.britta.sdmedia.se.', 'Textning: britta.sdmedia.se.', 'Whisper, Kungliga Biblioteket.', '[Almars stundar:']:
            self.assertEqual(pb.clean_dictation_text(text), '')
            self.assertEqual(pb.clean_dictation_text('Gör knappen större. ' + text), 'Gör knappen större.')

    def test_real_mentions_and_urls_are_preserved(self):
        for text in ['Öppna britta.sdmedia.se.', 'britta.sdmedia.se och så vidare.', 'Vi använder Whisper från Kungliga biblioteket.', 'Besök skyttberg.nu.', 'Whisper, Kungliga Biblioteket, är det vi använder.']:
            self.assertEqual(pb.clean_dictation_text(text), text)


class LanguageRequestTest(unittest.TestCase):
    def test_english_uses_english_model_on_separate_port(self):
        model, port = pb.recognition_defaults('en')
        self.assertTrue(model.endswith('/models/ggml-medium.en-q5_0.bin'))
        self.assertNotEqual(port, pb.DEFAULT_PORT)
        swedish_model, swedish_port = pb.recognition_defaults('sv')
        self.assertTrue(swedish_model.endswith('/models/ggml-kb-whisper-medium-q5_0.bin'))
        self.assertEqual(swedish_port, pb.DEFAULT_PORT)

    def test_language_is_sent_on_each_request_to_shared_server(self):
        import json
        for language in ('sv', 'en'):
            response = io.BytesIO(json.dumps({'text': 'test'}).encode())
            with patch.object(pb.urllib.request, 'urlopen', return_value=response) as request:
                pb.WhisperServer('unused', language=language).transcribe(b'RIFF')
                body = request.call_args.args[0].data
                self.assertIn(b'name="language"\r\n\r\n' + language.encode(), body)


class FakeLink:
    def __init__(self):
        self.sent = []

    def send(self, line):
        self.sent.append(line)


class FakeRecorder:
    def __init__(self, rate=48000):
        self.rate = rate
        self.capturing = False
        self.pending = b""

    def start(self):
        self.capturing = True

    def take(self):
        d, self.pending = self.pending, b""
        return d

    def stop(self):
        self.capturing = False


class FakeTranscriber:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def transcribe(self, wav):
        self.calls.append(wav)
        return self.text


class FakeTyper:
    def __init__(self):
        self.typed = []

    def type(self, text):
        self.typed.append(text)

    def key(self, name):
        self.typed.append(f"<{name}>")


def pcm(seconds, rate=48000, level=3000):
    """Syntetiskt 'tal': fyrkantsvåg med given amplitud. level=0 ger tystnad."""
    n = int(seconds * rate)
    vals = [level if (i // 24) % 2 == 0 else -level for i in range(n)]
    return struct.pack("<%dh" % n, *vals)


class SegmenterTest(unittest.TestCase):
    def make(self):
        return pb.Segmenter(rate=48000, min_silence_ms=500, max_segment_ms=8000, min_speech_ms=200, min_cut_ms=600)

    def test_paus_ger_segment(self):
        seg = self.make()
        out = seg.push(pcm(0.3, level=0)) + seg.push(pcm(1.0)) + seg.push(pcm(0.3, level=0))
        self.assertEqual(out, [])
        out = seg.push(pcm(0.3, level=0))  # nu > 500 ms tystnad
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(len(out[0]) / 2 / 48000, 1.0)   # innehåller talet
        self.assertLessEqual(len(out[0]) / 2 / 48000, 1.8)      # men inte all tystnad

    def test_kort_blipp_ignoreras(self):
        seg = self.make()
        out = seg.push(pcm(0.05)) + seg.push(pcm(1.0, level=0))
        self.assertEqual(out, [])
        self.assertEqual(seg.flush(), None)

    def test_langt_tal_klipps_vid_max(self):
        seg = self.make()
        out = []
        for _ in range(10):
            out += seg.push(pcm(1.0))
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(len(out[0]) / 2 / 48000, 8.0, delta=0.3)

    def test_flush_ger_resten(self):
        seg = self.make()
        seg.push(pcm(0.6))
        rest = seg.flush()
        self.assertIsNotNone(rest)
        self.assertAlmostEqual(len(rest) / 2 / 48000, 0.6, delta=0.1)
        self.assertEqual(seg.flush(), None)

    def test_kort_ord_klipps_inte_av_paus(self):
        seg = self.make()
        out = seg.push(pcm(0.3)) + seg.push(pcm(0.7, level=0)) + seg.push(pcm(0.5))
        self.assertEqual(out, [])            # 0,3 s tal räcker inte för pausklipp
        out = seg.push(pcm(0.7, level=0))
        self.assertEqual(len(out), 1)        # 0,8 s tal totalt → klipps vid nästa paus

    def test_tystnad_ger_inget(self):
        seg = self.make()
        self.assertEqual(seg.push(pcm(3.0, level=0)), [])
        self.assertEqual(seg.flush(), None)


class BridgeTest(unittest.TestCase):
    def make(self, text="hej världen"):
        link, rec, tr, ty = FakeLink(), FakeRecorder(), FakeTranscriber(text), FakeTyper()
        b = pb.Bridge(link, rec, tr, ty, log=lambda *a: None)
        return b, link, rec, tr, ty

    def feed(self, b, rec, data):
        rec.pending = data
        b.poll()

    def test_fras_skrivs_medan_luren_ar_lyft(self):
        b, link, rec, tr, ty = self.make()
        b.handle_line("DOWN")
        self.assertTrue(rec.capturing)
        self.feed(b, rec, pcm(1.0) + pcm(0.7, level=0))
        b.drain()
        self.assertEqual(ty.typed, ["hej världen "])       # skrivet före UP
        self.assertEqual(link.sent, ["BUSY"])
        b.handle_line("UP")
        b.drain()
        self.assertEqual(ty.typed, ["hej världen ", "<enter>"])   # Enter vid påläggning
        self.assertEqual(link.sent, ["BUSY", "IDLE", "PLAY klart"])

    def test_rest_vid_pa_laggning(self):
        b, link, rec, tr, ty = self.make()
        b.handle_line("DOWN")
        self.feed(b, rec, pcm(1.0) + pcm(0.7, level=0) + pcm(0.8))
        b.drain()
        self.assertEqual(len(ty.typed), 1)
        b.handle_line("UP")
        b.drain()
        self.assertEqual(len(ty.typed), 3)   # två fraser + Enter
        self.assertEqual(ty.typed[-1], "<enter>")
        self.assertEqual(len(tr.calls), 2)
        self.assertEqual(link.sent[-1], "PLAY klart")

    def test_credit_only_does_not_type_or_press_enter(self):
        b, link, rec, tr, ty = self.make('britta.sdmedia.se.')
        b.handle_line('DOWN')
        self.feed(b, rec, pcm(.3) + pcm(1.2, level=0))
        b.handle_line('UP')
        b.drain()
        self.assertEqual(ty.typed, [])
        self.assertEqual(len(tr.calls), 1)
        with wave.open(io.BytesIO(tr.calls[0])) as wav:
            self.assertLess(wav.getnframes()/wav.getframerate(), .6)
        self.assertEqual(link.sent[-1], 'PLAY inget')

    def test_inget_tal_ger_play_inget(self):
        b, link, rec, tr, ty = self.make()
        b.handle_line("DOWN")
        self.feed(b, rec, pcm(1.0, level=0))
        b.handle_line("UP")
        b.drain()
        self.assertEqual(tr.calls, [])
        self.assertEqual(ty.typed, [])                       # ingen Enter utan text
        self.assertEqual(link.sent, ["IDLE", "PLAY inget"])

    def test_hello_up_avbryter(self):
        b, link, rec, tr, ty = self.make()
        b.handle_line("HELLO DOWN")
        self.assertTrue(rec.capturing)
        b.handle_line("HELLO UP")
        self.assertFalse(rec.capturing)
        b.drain()
        self.assertEqual(tr.calls, [])

    def test_up_utan_down_gor_inget(self):
        b, link, rec, tr, ty = self.make()
        b.handle_line("UP")
        b.drain()
        self.assertEqual(tr.calls, [])
        self.assertEqual(link.sent, [])

    def test_tom_transkribering_skriver_inget(self):
        b, link, rec, tr, ty = self.make(text="  [BLANK_AUDIO] ")
        b.handle_line("DOWN")
        self.feed(b, rec, pcm(1.0))
        b.handle_line("UP")
        b.drain()
        self.assertEqual(ty.typed, [])
        self.assertEqual(link.sent, ["BUSY", "IDLE", "PLAY inget"])

    def test_ingen_enter_med_flaggan_av(self):
        link, rec, tr, ty = FakeLink(), FakeRecorder(), FakeTranscriber("hej"), FakeTyper()
        b = pb.Bridge(link, rec, tr, ty, log=lambda *a: None, enter_on_hangup=False)
        b.handle_line("DOWN")
        rec.pending = pcm(1.0)
        b.poll()
        b.handle_line("UP")
        b.drain()
        self.assertEqual(ty.typed, ["hej "])

    def test_inga_ljud_med_sounds_false(self):
        link, rec, tr, ty = FakeLink(), FakeRecorder(), FakeTranscriber("hej"), FakeTyper()
        b = pb.Bridge(link, rec, tr, ty, log=lambda *a: None, sounds=False)
        b.handle_line("DOWN")
        rec.pending = pcm(1.0)
        b.poll()
        b.handle_line("UP")
        b.drain()
        self.assertEqual(link.sent, ["BUSY", "IDLE"])

    def test_idle_skickas_aven_om_transkribering_kraschar(self):
        b, link, rec, tr, ty = self.make()

        def boom(wav):
            raise RuntimeError("server nere")

        tr.transcribe = boom
        b.handle_line("DOWN")
        self.feed(b, rec, pcm(1.0))
        b.handle_line("UP")
        b.drain()
        self.assertEqual(link.sent, ["BUSY", "IDLE", "PLAY inget"])
        self.assertEqual(ty.typed, [])

    def test_wav_ar_giltig_mono_16bit(self):
        data = pcm(0.5)
        w = pb.wav_bytes(data, 48000)
        with wave.open(io.BytesIO(w)) as f:
            self.assertEqual(f.getnchannels(), 1)
            self.assertEqual(f.getsampwidth(), 2)
            self.assertEqual(f.getframerate(), 48000)
            self.assertEqual(f.getnframes(), 24000)

    def test_clean_text(self):
        self.assertEqual(pb.clean_text("  Hej,   du.\n"), "Hej, du.")
        self.assertEqual(pb.clean_text("[BLANK_AUDIO]"), "")
        self.assertEqual(pb.clean_text("(tystnad)"), "")
        self.assertEqual(pb.clean_text("Åsa åt ärtor"), "Åsa åt ärtor")

    def test_multipart_innehaller_fil_och_falt(self):
        ctype, body = pb.multipart({"response_format": "json", "temperature": "0"}, "file", "a.wav", b"RIFFxx")
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        boundary = ctype.split("boundary=")[1].encode()
        self.assertIn(b'name="file"; filename="a.wav"', body)
        self.assertIn(b"RIFFxx", body)
        self.assertIn(b'name="response_format"\r\n\r\njson', body)
        self.assertTrue(body.endswith(b"--" + boundary + b"--\r\n"))

    def test_hitta_port_pa_vid_pid(self):
        class P:
            def __init__(self, dev, vid, pid, iface=None):
                self.device, self.vid, self.pid, self.interface = dev, vid, pid, iface

        ports = [P("/dev/tty.usbmodem1", 0x2E8A, 0x000A), P("/dev/tty.usbmodem2", 0xCAFE, 0x4011, "Phone Prompter")]
        self.assertEqual(pb.pick_port(ports), "/dev/tty.usbmodem2")
        self.assertIsNone(pb.pick_port(ports[:1]))

    def test_parse_server_svar(self):
        self.assertEqual(pb.parse_inference_json(b'{"text": " Hej d\\u00e5 "}'), " Hej då ")


if __name__ == "__main__":
    unittest.main()


class EftertextTest(unittest.TestCase):
    """Whisper hittar på undertextar-eftertexter när ljudet tar slut.

    Fallen nedan är hämtade ur den riktiga loggen: 7 av 459 transkriberingar
    under skarp användning. Filtret krävde tidigare exakt britta.sdmedia.se,
    men modellen hittar på vilket namn och vilken domän som helst.
    """

    VERKLIGA = [
        "Textning: Maria Grimstad www.sdimedia.com",
        "Textning: Lisa Hagström www.sdimedia.com",
        "Textning: Svensk Medietext för TV",
        "Textning: Svensk Medietext för SVT",
        "Whisper, Kungliga Biblioteket.",
        "[Almars stundar:",
    ]

    # "britta.sdmedia.se och så vidare." dök också upp i loggen men står
    # medvetet INTE här: befintliga tester kräver att inbäddade omnämnanden
    # bevaras, eftersom det kan vara riktig diktering.

    def test_skrapet_forsvinner_helt(self):
        for skrap in self.VERKLIGA:
            self.assertEqual(pb.clean_dictation_text(skrap), "", f"kvar: {skrap!r}")

    def test_skrapet_forsvinner_efter_riktig_text(self):
        for skrap in self.VERKLIGA:
            rad = "Boka ett möte på tisdag. " + skrap
            self.assertEqual(pb.clean_dictation_text(rad), "Boka ett möte på tisdag.",
                             f"misslyckades för {skrap!r}")

    def test_riktig_text_lamnas_ifred(self):
        # Får inte äta upp vanlig diktering bara för att ett ord liknar.
        for text in ("Boka ett möte med Gösta klockan två.",
                     "Vi behöver textning av filmen till fredag.",
                     "Fråga om undertexter finns på svenska.",
                     "Skicka en översättning av offerten.",
                     "Textning är dyrt."):
            self.assertEqual(pb.clean_dictation_text(text), text)


class SvansTrimTest(unittest.TestCase):
    """Sista tiondelarna fångar luren som läggs på.

    Mätt i loggen: 8 av 9 påhittade eftertexter kom i SISTA frasen före
    "pålagt", med bara 170–400 ms detekterat tal i segment på 0,4–1,4 s.
    Det är klingandet från klykan, inte tal.
    """

    def test_trimmar_bort_sista_millisekunderna(self):
        rate = 48000
        pcm = b"\x01\x02" * rate          # 1 sekund, 16-bitars mono
        kvar = pb.trimma_svans(pcm, rate, 200)
        self.assertEqual(len(kvar), int(rate * 2 * 0.8))

    def test_noll_ms_lamnar_orort(self):
        pcm = b"\x01\x02" * 1000
        self.assertEqual(pb.trimma_svans(pcm, 48000, 0), pcm)

    def test_kortare_ljud_an_trimmet_blir_tomt(self):
        # Får inte kasta, och får inte ge negativa index.
        pcm = b"\x01\x02" * 100
        self.assertEqual(pb.trimma_svans(pcm, 48000, 200), b"")

    def test_tomt_ljud_klarar_sig(self):
        self.assertEqual(pb.trimma_svans(b"", 48000, 200), b"")

    def test_trimmet_foljer_samplingsfrekvensen(self):
        # Telefonen kör 8 kHz, micken 48 kHz — lika lång tid, olika antal byte.
        atta = pb.trimma_svans(b"\x00" * 8000 * 2, 8000, 200)
        fyrtioatta = pb.trimma_svans(b"\x00" * 48000 * 2, 48000, 200)
        self.assertEqual(len(atta), int(8000 * 2 * 0.8))
        self.assertEqual(len(fyrtioatta), int(48000 * 2 * 0.8))

    def test_udda_bytelangd_ger_jamnt_resultat(self):
        # 16-bitars prov måste förbli hela, annars blir ljudet brus.
        kvar = pb.trimma_svans(b"\x00" * 1001, 8000, 10)
        self.assertEqual(len(kvar) % 2, 0)


class TextPrefixTest(unittest.TestCase):
    """Whisper skriver både "Textning:" och "Text:" före krediten."""

    def test_text_prefix_fangas(self):
        self.assertEqual(
            pb.clean_dictation_text("Text: Britt Borgström www.sdimedia.com"), "")

    def test_text_prefix_efter_riktig_mening(self):
        self.assertEqual(
            pb.clean_dictation_text("Ring kunden imorgon. Text: Britt Borgström www.sdimedia.com"),
            "Ring kunden imorgon.")


class TrimStandardTest(unittest.TestCase):
    """Värdet är mätt, inte valt på känsla — det ska inte glida tyst."""

    def test_standard_ar_400_ms(self):
        # 200 ms räckte inte: en svensk session med trimningen aktiv fick
        # ändå "Text: Britt Borg" ur ett segment med 170 ms tal.
        self.assertEqual(pb.bygg_argparser().parse_args([]).trim_tail_ms, 400)

    def test_gar_att_stanga_av(self):
        self.assertEqual(pb.bygg_argparser().parse_args(["--trim-tail-ms", "0"]).trim_tail_ms, 0)


class LangPausTest(unittest.TestCase):
    """En lång paus är en meningsgräns, även om lite tal hunnit räknas.

    Verkligt fall ur loggen: segment på 4,0 s med 350 ms tal och 3100 ms
    tystnad inuti. Spärren min_cut hindrade pausen från att klippa, så
    segmentet växte till maxlängden och kapade mitt i meningen.
    """

    def blk(self, seg, rms, ms):
        """Block med ungefär önskad RMS."""
        import array
        n = seg.block * (ms // seg.BLOCK_MS)
        return array.array("h", [rms, -rms] * (n // 2)).tobytes()

    def segmenterare(self, **kw):
        return pb.Segmenter(rate=8000, min_silence_ms=350, max_segment_ms=4000, **kw)

    def test_lang_paus_klipper_trots_lite_tal(self):
        seg = self.segmenterare()
        ut = []
        ut += seg.push(self.blk(seg, 3000, 300))     # kort tal, under min_cut 600 ms
        ut += seg.push(self.blk(seg, 2, 1500))       # lång tystnad
        self.assertTrue(ut, "lång paus klippte inte")

    def test_kort_paus_klipper_inte_mitt_i_ord(self):
        # Spärren ska finnas kvar: korta andningspauser får inte hacka sönder tal.
        seg = self.segmenterare()
        ut = seg.push(self.blk(seg, 3000, 200))
        ut += seg.push(self.blk(seg, 2, 400))        # kort paus, lite tal
        self.assertEqual(ut, [], "kort paus klippte fast den inte borde")

    def test_segment_nar_inte_maxlangden_vid_lang_tystnad(self):
        seg = self.segmenterare()
        seg.push(self.blk(seg, 3000, 300))
        seg.push(self.blk(seg, 2, 1500))
        seg.push(self.blk(seg, 3000, 300))
        # Utan regeln hade allt legat kvar i ett växande segment
        self.assertLess(len(seg.seg) * seg.BLOCK_MS, 4000)


class MaxlangdTest(unittest.TestCase):
    """Maxlängden ska vara en säkerhetsspärr, inte det som normalt klipper.

    Verkligt fall: två meningar på 4,6 och 6,9 sekunder slog båda i
    maxlängden 4,0 s och kapades mitt i — 'bastustunden' blev 'bastu.' +
    'stunden.', och resten av första meningen hamnade i ett segment där
    modellen hittade på text runt den.
    """

    def test_standard_rymmer_en_vanlig_mening(self):
        # En talad mening tar ofta 5–7 sekunder.
        self.assertGreaterEqual(pb.bygg_argparser().parse_args([]).max_phrase, 7.0)

    def test_paus_klipper_fortfarande_forst(self):
        # Höjd maxlängd får inte betyda längre väntan när man pausar normalt.
        import array
        rate = 8000
        seg = pb.Segmenter(rate=rate, min_silence_ms=350, max_segment_ms=8000)
        blk = lambda rms, ms: array.array(
            "h", [rms, -rms] * ((rate * 10 // 1000) * (ms // 10) // 2)).tobytes()
        ut = seg.push(blk(900, 1000))      # tal
        ut += seg.push(blk(3, 500))        # paus över min_silence
        self.assertEqual(len(ut), 1, "pausen klippte inte")
        self.assertLess(len(ut[0]) / 2 / rate, 2.0, "segmentet växte till maxlängden")


class BinarSokvagTest(unittest.TestCase):
    """whisper-server måste hittas även utan Homebrew i PATH.

    En app som startas från Finder får PATH=/usr/bin:/bin:/usr/sbin:/sbin,
    och /opt/homebrew/bin saknas. Felet låg latent tills språket byttes:
    den nya porten hade ingen server igång, bryggan försökte starta en och
    kraschade med FileNotFoundError.
    """

    def test_hittar_via_path_nar_den_finns(self):
        import shutil
        from unittest.mock import patch
        with patch.object(shutil, "which", return_value="/nagonstans/whisper-server"):
            self.assertEqual(pb.hitta_binar("whisper-server"), "/nagonstans/whisper-server")

    def test_faller_tillbaka_pa_kanda_platser(self):
        import shutil
        from unittest.mock import patch
        import os
        with patch.object(shutil, "which", return_value=None), \
             patch.object(os.path, "exists", lambda p: p == "/opt/homebrew/bin/whisper-server"):
            self.assertEqual(pb.hitta_binar("whisper-server"),
                             "/opt/homebrew/bin/whisper-server")

    def test_ger_namnet_tillbaka_om_inget_hittas(self):
        # Då får felmeddelandet från Popen tala, med kommandot synligt.
        import shutil, os
        from unittest.mock import patch
        with patch.object(shutil, "which", return_value=None), \
             patch.object(os.path, "exists", lambda p: False):
            self.assertEqual(pb.hitta_binar("whisper-server"), "whisper-server")
