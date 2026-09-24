# Phone Prompter

Lyft luren, tala, och texten knappas in på din Mac. Transkriberingen sker
lokalt — inget ljud lämnar datorn.

Två ljudkällor stöds: en **USB-mikrofon** med push-to-talk-knapp, eller en
**SIP-bordstelefon** som sänder multicast-paging på nätverket. Menyraden
väljer mellan dem.

## Hur det fungerar

```
ljudkälla ──▶ segmenterare ──▶ whisper.cpp ──▶ tangentbord
                  │
            klipper vid pauser
```

Segmenteraren klipper talet i fraser vid pauser och skickar varje fras till
en lokal transkriberingsserver. Texten skrivs som tangenttryck där markören
står, så den fungerar i vilket program som helst.

Menyradsappen (☎) startar och övervakar bryggan, startar om den om den dör,
och startar själv vid inloggning.

## Kom igång

```bash
cd bridge
./setup.sh              # hämtar talmodeller (KB-Whisper för svenska)
./install_menubar.sh    # menyradsapp som startar vid inloggning
```

Kräver `whisper-server` i PATH (`brew install whisper-cpp`) och
hjälpmedelsbehörighet för att få skriva tangenttryck.

## Ljudkällor

**USB-mikrofon.** En mikrofon som exponerar en serieport med knapptryck
(`DOWN`/`UP`). Byggd för en Pico 2 W med INMP441, men protokollet är enkelt
nog för annan hårdvara.

**Bordstelefon via multicast.** En SIP-telefon som kan sända multicast-paging
strömmar RTP direkt på LAN:et — ingen server, ingen SIP-registrering, ingen
molntjänst. Bryggan lyssnar på gruppen och avkodar G.711.

```bash
python3 ptt_bridge.py --multicast --multicast-kalla 192.168.1.215
```

Testad med Panasonic KX-HDV230. Telefonen behöver `MPAGE_*`-inställningarna
satta till en multicast-adress och PCMA som codec.

## Transkriberare

**KB-Whisper** (standard) via whisper.cpp. Snabb och träffsäker på svenska.

**Pianissimo-sv** (valfri) — en svensk FastConformer via NeMo. Långsammare
och tar 5,5 GB minne, men returnerar tom sträng på tystnad i stället för att
hitta på text. Installeras med `./setup-pianissimo.sh` och väljs i menyraden.

Jämför dem på eget ljud med `jamfor_modeller.py`.

## Det som kostat tid att lära sig

**Modeller hittar på text ur tystnad.** KB-Whisper är tränad på undertextat
material och fyller tysta segment med `Tack`, `Ja.`, `Musik`,
`TELEFONSIGNAL` och undertextar-krediter. Av 505 transkriberingar i skarp
drift hade 81 högst 400 ms detekterat tal, och de blev sådant. `Tack` och
`Ja.` går inte att filtrera bort — de ser ut som riktiga ord.

Tre motmedel finns: `--trim-tail-ms` klipper slutet av inspelningen (luren i
klykan låter som tal), ett filter fångar kreditrader, och Pianissimo undviker
problemet helt.

**Längsta fras var roten till det mesta.** Med maxlängd 4 sekunder kapades
vanliga meningar mitt i — "bastustunden" blev "bastu." + "stunden." — och de
avkapade svansarna hamnade i korta segment som modellen fyllde med påhitt.
Höjd till 8 sekunder blev samma uppläsning två hela, felfria meningar.
Maxlängden är en spärr; pausen klipper normalt först.

**Live-utskrift medan man talar går inte med Whisper.** Transkribering tar
0,24–0,45 s oavsett ljudlängd, så att köra om en växande buffert vore
billigt, och orden kunde skrivas 2,7–3,5 s tidigare. Men modellen hör
självsäkert fel ur halvt ljud — "du cyklar" blir "det fungerar" först med
hela meningen — och ingen stabilitetsregel hjälpte. Whisper är tränad på
30-sekundersfönster och är inte byggd för inkrementell avkodning.

**`audioop` finns inte i Python 3.13+.** G.711 avkodas med egna tabeller i
`g711.py`.

**Testerna körs inifrån `bridge/`**, inte från reporoten:

```bash
cd bridge && python3 -m unittest test_bridge.py test_multicast.py \
    test_menubar.py test_pianissimo.py
```

## Licens

MIT.
