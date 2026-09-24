#!/usr/bin/env python3
"""Demoskärm: visar det du säger som levande undertext, ord för ord.

Bryggan skickar två sorters händelser hit. *Preliminär* text kommer medan
du fortfarande talar och kan ändras; *slutlig* text är frasen som faktiskt
knappas in. Skärmen visar skillnaden med färg och rörelse — preliminära ord
är mjuka och cyan, fastställda knäpper till i vitt.

Att visa preliminär text är ofarligt, till skillnad från att knappa in den:
en skärm kan byta ut ett ord, ett tangentbord kan inte ta tillbaka det.

    python3 ptt_bridge.py --demo
"""
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SIDA = Path(__file__).with_name("demo.html")


class Demoskarm:
    """HTTP-server med en händelseström till webbläsaren.

    Varje ansluten flik får en egen kö. En flik som stängs eller hänger sig
    får inte blockera bryggan, så köerna är begränsade och full kö slänger
    händelsen i stället för att vänta.
    """

    def __init__(self, port=8790, log=print):
        self.port = port
        self.log = log
        self.koer = []
        self.las = threading.Lock()
        self.server = None
        self.senaste_lage = {"lyssnar": False, "kalla": ""}
        # Pågående fras, så en flik som öppnas mitt i en mening ser den.
        self.senaste_text = None
        # Har demofliken tangentbordsfokus? Då ska texten inte knappas in —
        # den hade hamnat i webbläsaren i stället för där man arbetar.
        self._fokus_till = 0.0
        self.fokus_giltig_s = 6.0

    # --- händelser in från bryggan
    def preliminar(self, text):
        self.senaste_text = ("preliminar", text)
        self._sand("preliminar", {"text": text})

    def slutlig(self, text):
        self.senaste_text = ("slutlig", text)
        self._sand("slutlig", {"text": text})

    def lage(self, lyssnar, kalla=None):
        # Vid lyft och pålagt finns ingen fras att spela upp längre.
        self.senaste_text = None
        self.senaste_lage = {"lyssnar": lyssnar,
                             "kalla": kalla or self.senaste_lage["kalla"]}
        self._sand("lage", self.senaste_lage)

    def _sand(self, typ, data):
        rad = f"event: {typ}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self.las:
            for k in list(self.koer):
                try:
                    k.put_nowait(rad)
                except queue.Full:
                    pass          # flik som inte hänger med tappar hellre en bild

    def satt_fokus(self, pa):
        """Sidan säger till när den får och tappar fokus.

        Tiden är en spärr: stängs fliken utan att hinna säga ifrån slutar
        fokus gälla av sig självt, så utskriften aldrig dör tyst.
        """
        self._fokus_till = (time.monotonic() + self.fokus_giltig_s) if pa else 0.0

    def har_fokus(self):
        return time.monotonic() < self._fokus_till

    # --- server
    def starta(self):
        skarm = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/handelser"):
                    self._strom()
                elif self.path.startswith("/fokus"):
                    skarm.satt_fokus(self.path.endswith("=1"))
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path in ("/", "/index.html"):
                    self._sida()
                else:
                    self.send_error(404)

            def _sida(self):
                kropp = SIDA.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(kropp)))
                self.end_headers()
                self.wfile.write(kropp)

            def _strom(self):
                k = queue.Queue(maxsize=64)
                with skarm.las:
                    skarm.koer.append(k)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    # Nyansluten flik ska veta läget direkt, inte vid nästa fras.
                    lage = json.dumps(skarm.senaste_lage, ensure_ascii=False)
                    self.wfile.write(f"event: lage\ndata: {lage}\n\n".encode())
                    if skarm.senaste_text:
                        typ, text = skarm.senaste_text
                        d = json.dumps({"text": text}, ensure_ascii=False)
                        self.wfile.write(f"event: {typ}\ndata: {d}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            self.wfile.write(k.get(timeout=15).encode())
                        except queue.Empty:
                            self.wfile.write(b": puls\n\n")   # håller kopplingen vid liv
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with skarm.las:
                        if k in skarm.koer:
                            skarm.koer.remove(k)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                # En flik som stängs bryter kopplingen mitt i strömmen. Det är
                # normalt och ska inte skrika i loggen.
                pass

        self.server = Server(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.log(f"demoskärm på http://127.0.0.1:{self.port}")
        return self

    def stoppa(self):
        if self.server:
            self.server.shutdown()
            self.server = None


class DemoTyper:
    """Skickar texten till skärmen och vidare till den riktiga utskriften.

    Bryggan vet inget om demot — den anropar type() som vanligt.
    """

    def __init__(self, inre, skarm):
        self.inre = inre
        self.skarm = skarm

    def type(self, text):
        self.skarm.slutlig(text.strip())
        # Ligger fokus i demofliken skulle tangenttrycken hamna där.
        if not self.skarm.har_fokus():
            self.inre.type(text)

    def __getattr__(self, namn):
        return getattr(self.inre, namn)
