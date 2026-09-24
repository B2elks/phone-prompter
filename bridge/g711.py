#!/usr/bin/env python3
"""G.711-avkodning (a-law och µ-law) till 16-bitars PCM.

Skrivet för hand eftersom `audioop` togs bort ur standardbiblioteket i
Python 3.13 och Macen kör 3.14. Algoritmen är den i ITU-T G.711, samma som
referensimplementationen i CCITT G.711 (g711.c).

Medveten kopia av `kx-hdv230/g711.py`. Repot delar inte kod mellan
projektmappar — en import härifrån till kx-hdv230 skulle knyta ihop två
projekt som annars är oberoende. Ändras den ena bör den andra ses över.
"""
import array

_SIGN, _QUANT, _SEG, _SHIFT, _BIAS = 0x80, 0x0F, 0x70, 4, 0x84


def _alaw_tabell():
    tab = []
    for a in range(256):
        v = a ^ 0x55
        t = (v & _QUANT) << 4
        seg = (v & _SEG) >> _SHIFT
        if seg == 0:
            t += 8
        elif seg == 1:
            t += 0x108
        else:
            t += 0x108
            t <<= seg - 1
        tab.append(t if (v & _SIGN) else -t)
    return tab


def _ulaw_tabell():
    tab = []
    for u in range(256):
        v = (~u) & 0xFF
        t = ((v & _QUANT) << 3) + _BIAS
        t <<= (v & _SEG) >> _SHIFT
        tab.append(_BIAS - t if (v & _SIGN) else t - _BIAS)
    return tab


ALAW = _alaw_tabell()
ULAW = _ulaw_tabell()


def avkoda(nyttolast, payload_typ):
    """Returnerar 16-bitars PCM (little endian) eller None för okänd codec."""
    tab = {8: ALAW, 0: ULAW}.get(payload_typ)
    if tab is None:
        return None
    return array.array("h", [tab[b] for b in nyttolast]).tobytes()


def rms(pcm):
    if not pcm:
        return 0
    prov = array.array("h")
    prov.frombytes(pcm[:len(pcm) // 2 * 2])
    return int((sum(p * p for p in prov) / len(prov)) ** 0.5) if prov else 0
