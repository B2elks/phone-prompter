"""Tester för multicast-källan. Körs utan telefon, nätverk eller whisper:

    cd bridge && python3 -m unittest test_multicast.py
"""
import struct
import unittest

import g711
import multicast


class G711Test(unittest.TestCase):
    """Kontrollerar avkodaren mot värden ur ITU-T G.711."""

    def test_alaw_tystnad(self):
        self.assertEqual(g711.ALAW[0xD5], 8)

    def test_ulaw_tystnad(self):
        self.assertEqual(g711.ULAW[0xFF], 0)

    def test_spann(self):
        self.assertEqual((min(g711.ALAW), max(g711.ALAW)), (-32256, 32256))
        self.assertEqual((min(g711.ULAW), max(g711.ULAW)), (-32124, 32124))

    def test_avkodar_alaw_till_pcm(self):
        # 160 byte a-law blir 320 byte PCM: en 20 ms ram vid 8 kHz.
        pcm = g711.avkoda(bytes([0xD5]) * 160, 8)
        self.assertEqual(len(pcm), 320)

    def test_avkodar_ulaw_till_pcm(self):
        pcm = g711.avkoda(bytes([0xFF]) * 160, 0)
        self.assertEqual(len(pcm), 320)

    def test_okand_codec_ger_none(self):
        # G722 är payload-typ 9 och stöds inte — måste ge None, inte skräp.
        self.assertIsNone(g711.avkoda(bytes(160), 9))

    def test_rms_pa_tystnad_ar_lagt(self):
        self.assertLess(g711.rms(g711.avkoda(bytes([0xD5]) * 160, 8)), 20)

    def test_rms_pa_tom_indata(self):
        self.assertEqual(g711.rms(b""), 0)


if __name__ == "__main__":
    unittest.main()


def rtp(nyttolast=b"\xd5" * 160, payload_typ=8, sekvens=1):
    """Bygger ett RTP-paket: 12 byte huvud enligt RFC 3550, sedan nyttolast."""
    return struct.pack("!BBHII", 0x80, payload_typ, sekvens, 0, 0x1234) + nyttolast


class PaketStromTest(unittest.TestCase):
    """Strömmen matas direkt, utan uttag — testerna rör aldrig nätverket."""

    def strom(self):
        return multicast.PaketStrom("239.1.1.10", 5004, "192.168.1.165",
                                    "192.168.1.215")

    def test_rate_ar_8000(self):
        self.assertEqual(self.strom().rate, 8000)

    def test_giltigt_paket_ger_pcm(self):
        s = self.strom()
        s.mata(rtp(), "192.168.1.215")
        # 160 byte a-law blir 320 byte PCM
        self.assertEqual(len(s.ta_pcm()), 320)

    def test_ta_pcm_tommer_bufferten(self):
        s = self.strom()
        s.mata(rtp(), "192.168.1.215")
        s.ta_pcm()
        self.assertEqual(s.ta_pcm(), b"")

    def test_fel_avsandare_ignoreras(self):
        s = self.strom()
        s.mata(rtp(), "192.168.1.99")
        self.assertEqual(s.ta_pcm(), b"")

    def test_okand_codec_ignoreras(self):
        s = self.strom()
        s.mata(rtp(payload_typ=9), "192.168.1.215")   # G722
        self.assertEqual(s.ta_pcm(), b"")

    def test_for_kort_paket_kastas_utan_undantag(self):
        s = self.strom()
        s.mata(b"\x80\x08", "192.168.1.215")
        self.assertEqual(s.ta_pcm(), b"")

    def test_senaste_paket_uppdateras_bara_av_accepterade(self):
        s = self.strom()
        self.assertIsNone(s.senaste_paket)
        s.mata(rtp(), "192.168.1.99")
        self.assertIsNone(s.senaste_paket)
        s.mata(rtp(), "192.168.1.215")
        self.assertIsNotNone(s.senaste_paket)

    def test_senaste_paket_star_still_vid_avvisat_paket(self):
        s = self.strom()
        s.mata(rtp(), "192.168.1.215")
        forst = s.senaste_paket
        s.mata(rtp(payload_typ=9), "192.168.1.215")
        self.assertEqual(s.senaste_paket, forst)


class MulticastRecorderTest(unittest.TestCase):
    """Samma gränssnitt som SoundDeviceRecorder: rate, start, take, stop."""

    def setUp(self):
        self.strom = multicast.PaketStrom("239.1.1.10", 5004, "192.168.1.165",
                                          "192.168.1.215")
        self.rec = multicast.MulticastRecorder(self.strom)

    def test_rate_ar_8000(self):
        # Bryggan bär rate hela vägen till whisper; fel värde ger fel tonhöjd.
        self.assertEqual(self.rec.rate, 8000)

    def test_take_efter_start_ger_buffrat_ljud(self):
        self.rec.start()
        self.strom.mata(rtp(), "192.168.1.215")
        self.assertEqual(len(self.rec.take()), 320)

    def test_take_innan_start_ger_tomt(self):
        self.strom.mata(rtp(), "192.168.1.215")
        self.assertEqual(self.rec.take(), b"")

    def test_omstart_lamnar_inga_rester(self):
        # Annars läcker slutet av förra frasen in i början av nästa.
        self.rec.start()
        self.strom.mata(rtp(), "192.168.1.215")
        self.rec.stop()
        self.rec.start()
        self.assertEqual(self.rec.take(), b"")

    def test_take_tommer(self):
        self.rec.start()
        self.strom.mata(rtp(), "192.168.1.215")
        self.rec.take()
        self.assertEqual(self.rec.take(), b"")


class MulticastLinkTest(unittest.TestCase):
    """Paketflöde översatt till Bridge:s DOWN/UP.

    Klockan matas in, så testerna varken väntar eller blir flakiga.
    """

    def setUp(self):
        self.nu = 1000.0
        self.strom = multicast.PaketStrom("239.1.1.10", 5004, "192.168.1.165",
                                          "192.168.1.215")
        self.link = multicast.MulticastLink(self.strom, tystnad_ms=800,
                                            klocka=lambda: self.nu)

    def paket(self):
        self.strom.senaste_paket = self.nu

    def test_forsta_paketet_ger_down(self):
        self.paket()
        self.assertEqual(self.link.tick(), "DOWN")

    def test_down_ges_bara_en_gang(self):
        self.paket()
        self.link.tick()
        self.nu += 0.02
        self.paket()
        self.assertIsNone(self.link.tick())

    def test_tystnad_ger_up(self):
        self.paket()
        self.link.tick()
        self.nu += 0.9          # 900 ms > tröskeln 800 ms
        self.assertEqual(self.link.tick(), "UP")

    def test_up_ges_bara_en_gang(self):
        self.paket()
        self.link.tick()
        self.nu += 0.9
        self.link.tick()
        self.nu += 0.9
        self.assertIsNone(self.link.tick())

    def test_kort_uppehall_avslutar_inte_frasen(self):
        # Telefonen gör pauser mitt i meningar; de får inte klippa frasen.
        self.paket()
        self.link.tick()
        self.nu += 0.5          # 500 ms < tröskeln
        self.assertIsNone(self.link.tick())

    def test_nytt_tal_efter_up_ger_down_igen(self):
        self.paket()
        self.link.tick()
        self.nu += 0.9
        self.link.tick()                       # UP
        self.nu += 5.0
        self.paket()
        self.assertEqual(self.link.tick(), "DOWN")

    def test_tick_utan_paket_ger_none(self):
        self.assertIsNone(self.link.tick())

    def test_send_gor_inget_och_kastar_inte(self):
        self.paket()
        self.link.tick()
        self.link.send("PLAY klart")
        self.nu += 0.5
        self.assertIsNone(self.link.tick())   # tillståndet orört

    def test_lines_ger_samma_som_tick(self):
        self.paket()
        rader = multicast.MulticastLink(self.strom, tystnad_ms=800,
                                        klocka=lambda: self.nu).lines()
        self.assertEqual(next(rader), "DOWN")


class MainMulticastTest(unittest.TestCase):
    """Uppgift 5 rör delad kod — Pico-vägen får inte påverkas."""

    def test_flaggor_finns_med_rimliga_standardvarden(self):
        import ptt_bridge as pb
        ap = pb.bygg_argparser()
        args = ap.parse_args(["--multicast"])
        self.assertTrue(args.multicast)
        self.assertEqual(args.multicast_grupp, "239.1.1.10")
        self.assertEqual(args.multicast_port, 5004)
        self.assertEqual(args.multicast_kalla, "192.168.1.215")

    def test_utan_flaggan_ar_multicast_av(self):
        import ptt_bridge as pb
        self.assertFalse(pb.bygg_argparser().parse_args([]).multicast)

    def test_segmenteraren_foljer_inspelarens_rate(self):
        # Segmenter var hårdkodad till 48000; med 8 kHz-källa skulle den
        # klippa fraser på fel längd.
        import ptt_bridge as pb
        strom = multicast.PaketStrom("239.1.1.10", 5004, "192.168.1.165",
                                     "192.168.1.215")
        rec = multicast.MulticastRecorder(strom)
        seg = pb.Segmenter(rate=rec.rate, min_silence_ms=350, max_segment_ms=4000)
        self.assertEqual(seg.rate, 8000)

    def test_multicast_kraver_inte_serial(self):
        # pyserial behövs inte när ingen Pico är inblandad.
        import ptt_bridge as pb
        self.assertEqual(pb.fel_typer(multicast_lage=True), (OSError,))


class LokalIpTest(unittest.TestCase):
    """Macens IP ändras när nätet byts — den får inte vara hårdkodad."""

    def test_tomt_varde_ger_alla_granssnitt(self):
        # INADDR_ANY låter kärnan välja; annars dör bryggan med
        # "Can't assign requested address" så fort DHCP ger en ny adress.
        self.assertEqual(multicast.valj_lokal_ip(None), "0.0.0.0")
        self.assertEqual(multicast.valj_lokal_ip(""), "0.0.0.0")

    def test_angiven_adress_respekteras(self):
        self.assertEqual(multicast.valj_lokal_ip("192.168.1.165"), "192.168.1.165")

    def test_strommen_klarar_utelamnad_adress(self):
        s = multicast.PaketStrom("239.1.1.10", 5004, None, "192.168.1.215")
        self.assertEqual(s.lokal_ip, "0.0.0.0")
