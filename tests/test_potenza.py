"""Power peaks from the 15-minute samples, contract power from getSupplies."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.edistribuzione import potenza, raw_storage
from custom_components.edistribuzione.const import MAGNITUDE_PRELEVATA
from custom_components.edistribuzione.fasce import FUSO_ITALIA

from .conftest import POD_A

# Wednesday 2026-10-14, Italian time UTC+2: 10:00 UTC = 12:00 F1.
ORA_F1 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def test_kw_e_la_media_del_quarto_d_ora():
    assert potenza.kw(0.75) == 3.0


def test_ore_potenza_media_minimo_massimo():
    campioni = [(ORA_F1 + timedelta(minutes=15 * i), kwh) for i, kwh in enumerate([0.1, 0.2, 0.3, 0.4])]
    ((ora, media, minimo, massimo),) = potenza.ore_potenza(campioni)
    assert ora == ORA_F1
    assert (media, minimo, massimo) == pytest.approx((1.0, 0.4, 1.6))


def test_riepilogo_picco_del_giorno_e_del_mese():
    campioni = [
        (datetime(2026, 10, 2, 18, 0, tzinfo=UTC), 0.9),  # 3.6 kW on the 2nd, 20:00 F2
        (ORA_F1, 0.5),  # 2.0 kW on the 14th
        (ORA_F1 + timedelta(minutes=15), 0.6),  # 2.4 kW on the 14th
    ]
    r = potenza.riepilogo(campioni, disponibile_kw=3.3)
    assert r["giorno"] == "2026-10-14"
    assert r["mese"] == "2026-10"
    assert r["picco_giorno"] == {
        "kw": 2.4,
        "quando": "2026-10-14T12:15:00+02:00",
        "fascia": "F1",
        "percentuale_disponibile": 73,
    }
    assert r["picco_mese"]["kw"] == 3.6
    assert r["picco_mese"]["fascia"] == "F2"
    assert r["picco_mese"]["percentuale_disponibile"] == 109


def test_riepilogo_senza_potenza_disponibile_ne_dati():
    assert potenza.riepilogo([], 3.3) is None
    r = potenza.riepilogo([(ORA_F1, 0.5)], None)
    assert "percentuale_disponibile" not in r["picco_giorno"]


def test_campioni_dal_primo_del_mese_dell_ultimo_giorno(tmp_path):
    db = str(tmp_path / "curve.db")
    settembre = datetime(2026, 9, 30, 21, 45, tzinfo=UTC)  # 23:45 on Sept 30 in Italy
    ottobre = datetime(2026, 9, 30, 22, 0, tzinfo=UTC)  # 00:00 on Oct 1 in Italy
    raw_storage.upsert_campioni(db, POD_A, MAGNITUDE_PRELEVATA, [(settembre, 1.0), (ottobre, 2.0), (ORA_F1, 3.0)])
    assert raw_storage.campioni_mese_ultimo_giorno(db, POD_A, MAGNITUDE_PRELEVATA, FUSO_ITALIA) == [
        (ottobre, 2.0),
        (ORA_F1, 3.0),
    ]
    assert raw_storage.campioni_mese_ultimo_giorno(db, "ALTRO", MAGNITUDE_PRELEVATA, FUSO_ITALIA) == []


async def test_contratto_da_get_supplies_una_volta_al_giorno(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(pods=[POD_A])
    forniture = [
        {"IdPod": POD_A, "ContractualPower": 3, "AvailablePower": "3.3"},
        {"IdPod": "IT001E99999999", "ContractualPower": 6, "AvailablePower": 6.6},
    ]
    with patch.object(coordinator._api, "async_get_supplies", AsyncMock(return_value=forniture)) as chiamata:
        await coordinator._async_aggiorna_contratto()
        await coordinator._async_aggiorna_contratto()
    assert coordinator.contratto == {POD_A: {"impegnata": 3.0, "disponibile": 3.3}}
    assert chiamata.await_count == 1


async def test_contratto_non_letto_riprova_e_tiene_i_valori(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator.contratto = {POD_A: {"impegnata": 3.0, "disponibile": 3.3}}
    with patch.object(coordinator._api, "async_get_supplies", AsyncMock(side_effect=RuntimeError)):
        await coordinator._async_aggiorna_contratto()
    assert coordinator.contratto[POD_A]["impegnata"] == 3.0
    assert coordinator._contratto_letto_il is None
