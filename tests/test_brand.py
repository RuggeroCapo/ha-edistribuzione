"""Integration icon in brand/, served by Home Assistant 2026.3+.

HA looks for brand/icon.png (256x256) and brand/icon@2x.png (512x512) in the
integration's folder; with other sizes the icon looks blurred or out of scale
on the integrations page.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

BRAND = Path(__file__).parent.parent / "custom_components" / "edistribuzione" / "brand"
FIRMA_PNG = b"\x89PNG\r\n\x1a\n"


def _dimensioni_png(percorso: Path) -> tuple[int, int, int]:
    """(width, height, colour type) from the PNG's IHDR header."""
    dati = percorso.read_bytes()[:26]
    assert dati[:8] == FIRMA_PNG and dati[12:16] == b"IHDR", percorso.name
    larghezza, altezza = struct.unpack(">II", dati[16:24])
    return larghezza, altezza, dati[25]


@pytest.mark.parametrize(("nome", "lato"), [("icon.png", 256), ("icon@2x.png", 512)])
def test_icona_con_le_dimensioni_attese_da_home_assistant(nome, lato):
    larghezza, altezza, tipo_colore = _dimensioni_png(BRAND / nome)
    assert (larghezza, altezza) == (lato, lato)
    assert tipo_colore == 6, "serve un PNG RGBA, per gli angoli arrotondati trasparenti"
