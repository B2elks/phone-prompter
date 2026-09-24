"""Telefonens multicast-paging som ljudkälla för bryggan.

Panasonic KX-HDV230 kan sända rått RTP till en multicast-adress på LAN:et
utan att gå via SIP eller någon server. Den här modulen lyssnar på gruppen
och presenterar strömmen genom bryggans två befintliga gränssnitt:
inspelarens och Pico-länkens. Paketflödet är både ljudet och
tryck-och-tala-signalen.

Metodnamnen är engelska där de måste matcha `SoundDeviceRecorder` i
`ptt_bridge.py`; hela filen håller samma språk för att inte blanda.
"""
import socket
import struct
import threading
import time

import g711

RTP_HUVUD = 12          # RFC 3550: 12 byte fast huvud
RATE = 8000             # PCMA är alltid 8 kHz


def valj_lokal_ip(angiven):
    """Vilket gränssnitt som går med i multicast-gruppen.

    Tom adress betyder INADDR_ANY — kärnan väljer. Hårdkodad adress dör med
    "Can't assign requested address" så fort maskinen byter nät eller får en
    ny adress av DHCP, vilket hände 2026-09-18.
    """
    return angiven or "0.0.0.0"


class PaketStrom:
    """Äger uttaget och läsartråden, buffrar avkodat PCM.

    Paket avvisas om de kommer från fel avsändare, bär en codec vi inte
    avkodar, eller är för korta för att innehålla ett RTP-huvud. Bara
    accepterade paket flyttar `senaste_paket` — tidsstämpeln är det
    länkadaptern läser för att avgöra om någon talar.
    """

    def __init__(self, grupp, port, lokal_ip, kalla_ip, log=print):
        self.grupp = grupp
        self.port = port
        self.lokal_ip = valj_lokal_ip(lokal_ip)
        self.kalla_ip = kalla_ip
        self.log = log
        self.rate = RATE
        self.senaste_paket = None
        self._lock = threading.Lock()
        self._buffert = bytearray()
        self._sock = None
        self._trad = None
        self._kor = False

    # --- matning; anropas av tråden, och direkt av testerna
    def mata(self, data, avsandare_ip):
        """Tar emot ett paket. Returnerar True om det accepterades."""
        if avsandare_ip != self.kalla_ip:
            return False
        if len(data) <= RTP_HUVUD:
            return False
        payload_typ = data[1] & 0x7F
        pcm = g711.avkoda(data[RTP_HUVUD:], payload_typ)
        if pcm is None:
            return False
        with self._lock:
            self._buffert += pcm
        self.senaste_paket = time.monotonic()
        return True

    def ta_pcm(self):
        """Allt buffrat ljud, och tömmer bufferten."""
        with self._lock:
            data = bytes(self._buffert)
            self._buffert.clear()
        return data

    def rensa(self):
        with self._lock:
            self._buffert.clear()

    # --- nätverk
    def start(self):
        if self._sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # macOS kräver SO_REUSEPORT för att flera processer ska dela UDP-porten.
        # Utan den går det inte att lyssna med ett diagnosverktyg vid sidan av
        # bryggan medan den kör.
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", self.port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack("4s4s", socket.inet_aton(self.grupp),
                                 socket.inet_aton(self.lokal_ip)))
        s.settimeout(0.2)
        self._sock = s
        self._kor = True
        self._trad = threading.Thread(target=self._las, daemon=True)
        self._trad.start()

    def _las(self):
        while self._kor:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as fel:
                # Ett trasigt paket eller ett stängt uttag får inte döda
                # tråden och därmed tysta hela bryggan.
                if self._kor:
                    self.log("multicast: läsfel", fel)
                continue
            try:
                self.mata(data, addr[0])
            except Exception as fel:          # noqa: BLE001
                self.log("multicast: kunde inte tolka paket", fel)

    def stop(self):
        self._kor = False
        trad, self._trad = self._trad, None
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if trad is not None:
            trad.join(timeout=1.0)


class MulticastRecorder:
    """Inspelare ovanpå paketströmmen.

    Samma gränssnitt som `SoundDeviceRecorder` i `ptt_bridge.py`, så
    `Bridge` och `Segmenter` inte behöver veta varifrån ljudet kommer.
    Till skillnad från mikrofonen öppnas ingenting vid `start()` — strömmen
    läser hela tiden. `start()` betyder därför "börja räkna härifrån", och
    rensar det som buffrats under tystnaden före.
    """

    def __init__(self, strom):
        self.strom = strom
        self.rate = strom.rate
        self.aktiv = False

    def start(self):
        self.strom.rensa()
        self.aktiv = True

    def take(self):
        if not self.aktiv:
            return b""
        return self.strom.ta_pcm()

    def stop(self):
        self.aktiv = False


class MulticastLink:
    """Paketflödet som tryck-och-tala-signal.

    `Bridge.handle_line` förstår redan Pico-mickens ord: "DOWN" börjar spela
    in, "UP" avslutar frasen, och `None` är en tick som låter huvudloopen
    göra annat. Adaptern översätter paketflödet till samma ord, så bryggan
    inte behöver veta att ljudet kommer från en telefon.

    Telefonen gör själv uppehåll mitt i tal — mätningen 2026-09-18 gav 9,4 s
    ljud utspritt över 71,8 s. Tröskeln måste därför vara lång nog att
    överleva pauser inom en mening, men kort nog att texten inte dröjer.

    `send()` finns bara för att uppfylla gränssnittet; det finns ingen Pico
    att spela upp kvitteringsljud på.
    """

    def __init__(self, strom, tystnad_ms=800, klocka=time.monotonic, log=print):
        self.strom = strom
        self.tystnad = tystnad_ms / 1000.0
        self.klocka = klocka
        self.log = log
        self.talar = False

    def tick(self):
        """Ett steg: returnerar "DOWN", "UP" eller None."""
        senaste = self.strom.senaste_paket
        if senaste is None:
            return None
        tyst_sedan = self.klocka() - senaste
        if not self.talar and tyst_sedan < self.tystnad:
            self.talar = True
            return "DOWN"
        if self.talar and tyst_sedan >= self.tystnad:
            self.talar = False
            return "UP"
        return None

    def lines(self):
        """Generator i samma form som SerialLink.lines()."""
        while True:
            rad = self.tick()
            yield rad
            if rad is None:
                time.sleep(0.02)

    def send(self, rad):
        # Ingen Pico att skicka till. Tyst här, annars spammas loggen.
        pass
