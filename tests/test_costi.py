"""Costs: tariff from the options, hourly cost series, sensor summary, and the
coordinator writing/linking the cost statistic only when something changed."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.edistribuzione import coordinator as coord_mod
from custom_components.edistribuzione import costi, raw_storage
from custom_components.edistribuzione.const import (
    CONF_TARIFFA,
    CONF_TIPO_POD,
    MAGNITUDE_PRELEVATA,
    TIPO_POD_PRODUZIONE,
)

from .conftest import POD_A

MONORARIA = {
    "tipo": "monoraria",
    "prezzo": 0.10,
    "extra_kwh": 0.05,
    "quota_fissa_mese": 7.44,
    "potenza_kw": 3.0,
    "quota_potenza": 0.0,
    "iva": 10.0,
}
FASCE = {
    "tipo": "fasce",
    "prezzo_f1": 0.30,
    "prezzo_f2": 0.20,
    "prezzo_f3": 0.10,
    "iva": 0.0,
}

# Wednesday 2026-10-14, Italian time is UTC+2: 10:00 UTC = 12:00 F1,
# 18:00 UTC = 20:00 F2, 23:00 UTC = 01:00 (Thursday) F3.
MERCOLEDI_F1 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
MERCOLEDI_F2 = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
GIOVEDI_F3 = datetime(2026, 10, 14, 23, 0, tzinfo=UTC)


class TestTariffa:
    def test_nessuna_tariffa(self):
        assert costi.Tariffa.da_opzioni(None) is None
        assert costi.Tariffa.da_opzioni({}) is None
        assert costi.Tariffa.da_opzioni({"tipo": "nessuna"}) is None

    def test_monoraria_stesso_prezzo_in_ogni_fascia(self):
        t = costi.Tariffa.da_opzioni(MONORARIA)
        assert (t.prezzo_f1, t.prezzo_f2, t.prezzo_f3) == (0.10, 0.10, 0.10)
        assert t.prezzo_kwh(MERCOLEDI_F1) == pytest.approx(0.15)

    def test_fasce_prezzo_secondo_la_fascia(self):
        t = costi.Tariffa.da_opzioni(FASCE)
        assert t.prezzo_kwh(MERCOLEDI_F1) == 0.30
        assert t.prezzo_kwh(MERCOLEDI_F2) == 0.20
        assert t.prezzo_kwh(GIOVEDI_F3) == 0.10

    def test_quota_potenza_per_kw(self):
        t = costi.Tariffa.da_opzioni({**MONORARIA, "potenza_kw": 4.5, "quota_potenza": 2.0})
        assert t.fisso_mese == pytest.approx(7.44 + 9.0)


class TestOreNelMese:
    @pytest.mark.parametrize(
        ("istante", "ore"),
        [
            (datetime(2026, 9, 15, tzinfo=UTC), 720),
            (datetime(2026, 10, 15, tzinfo=UTC), 745),  # clock goes back
            (datetime(2027, 3, 15, tzinfo=UTC), 743),  # clock goes forward
            (datetime(2026, 2, 10, tzinfo=UTC), 672),
        ],
    )
    def test_ore(self, istante, ore):
        assert costi.ore_nel_mese(istante) == ore

    def test_conta_il_mese_italiano_non_quello_utc(self):
        # 2026-09-30 23:00 UTC is already October 1 in Italy.
        assert costi.ore_nel_mese(datetime(2026, 9, 30, 23, tzinfo=UTC)) == 745


class TestCostiOrari:
    def test_somma_energia_per_ora_secondo_la_fascia(self):
        t = costi.Tariffa.da_opzioni(FASCE)
        campioni = [
            (MERCOLEDI_F1, 1.0),
            (MERCOLEDI_F1 + timedelta(minutes=15), 1.0),
            (MERCOLEDI_F2, 2.0),
        ]
        assert costi.costi_orari(campioni, t) == [
            (MERCOLEDI_F1, pytest.approx(0.60)),
            (MERCOLEDI_F2, pytest.approx(0.40)),
        ]

    def test_quota_fissa_spalmata_sulle_ore_e_iva(self):
        t = costi.Tariffa.da_opzioni(MONORARIA)
        ((_, euro),) = costi.costi_orari([(MERCOLEDI_F1, 0.0)], t)
        assert euro == pytest.approx(7.44 / 745 * 1.10)

    def test_un_mese_intero_somma_una_quota_fissa(self):
        t = costi.Tariffa.da_opzioni({**MONORARIA, "iva": 0.0})
        inizio = datetime(2026, 9, 1, tzinfo=costi.fasce.FUSO_ITALIA).astimezone(UTC)
        campioni = [(inizio + timedelta(hours=h), 0.0) for h in range(720)]
        assert sum(e for _, e in costi.costi_orari(campioni, t)) == pytest.approx(7.44)


class TestRiepilogo:
    def test_vuoto(self):
        assert costi.riepilogo([]) is None

    def test_ultimo_giorno_e_mese_fino_a_quel_giorno(self):
        ore = [
            (datetime(2026, 9, 30, 10, tzinfo=UTC), 5.0),  # previous month
            (datetime(2026, 10, 12, 10, tzinfo=UTC), 1.0),
            (datetime(2026, 10, 13, 10, tzinfo=UTC), 2.0),
            (datetime(2026, 10, 13, 21, tzinfo=UTC), 0.5),  # 23:00 Italian time
        ]
        assert costi.riepilogo(ore) == {
            "giorno": "2026-10-13",
            "costo_giorno": 2.5,
            "mese": "2026-10",
            "costo_mese": 3.5,
        }


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


@pytest.fixture
def _db(tmp_path):
    percorso = str(tmp_path / "curve.db")
    raw_storage.upsert_campioni(
        percorso,
        POD_A,
        MAGNITUDE_PRELEVATA,
        [(MERCOLEDI_F1, 1.0), (MERCOLEDI_F2, 2.0)],
    )
    return percorso


@pytest.fixture
def _registro(hass):
    """Pretend the recorder is loaded and record what would be written."""
    hass.config.components.add("recorder")
    instance = MagicMock()
    with (
        patch.object(coord_mod, "async_scrivi_costi") as scrivi,
        patch(
            "custom_components.edistribuzione.energy_dashboard.async_collega_costo",
            AsyncMock(return_value=True),
        ) as collega,
        patch.object(coord_mod, "get_instance", return_value=instance),
    ):
        yield MagicMock(scrivi=scrivi, collega=collega, clear=instance.async_clear_statistics)


async def test_scrive_e_collega_il_costo_solo_quando_cambia(
    hass, make_edist_coordinator, _db, _registro
):
    coordinator = make_edist_coordinator(pods=[POD_A], options={CONF_TARIFFA: FASCE})

    sintesi = await coordinator._async_aggiorna_costi(POD_A, _db)
    assert sintesi == {
        "giorno": "2026-10-14",
        "costo_giorno": 0.7,
        "mese": "2026-10",
        "costo_mese": 0.7,
    }
    assert _registro.scrivi.call_count == 1
    _registro.collega.assert_awaited_once_with(hass, POD_A, attivo=True)

    # Same samples, same tariff: nothing rewritten.
    assert await coordinator._async_aggiorna_costi(POD_A, _db) == sintesi
    assert _registro.scrivi.call_count == 1

    # A corrected sample: rewritten.
    raw_storage.upsert_campioni(_db, POD_A, MAGNITUDE_PRELEVATA, [(MERCOLEDI_F2, 3.0)])
    nuova = await coordinator._async_aggiorna_costi(POD_A, _db)
    assert nuova["costo_giorno"] == 0.9
    assert _registro.scrivi.call_count == 2


async def test_senza_tariffa_pulisce_una_volta(hass, make_edist_coordinator, _db, _registro):
    coordinator = make_edist_coordinator(pods=[POD_A])
    assert await coordinator._async_aggiorna_costi(POD_A, _db) is None
    assert await coordinator._async_aggiorna_costi(POD_A, _db) is None
    _registro.collega.assert_awaited_once_with(hass, POD_A, attivo=False)
    assert _registro.clear.call_count == 1
    _registro.scrivi.assert_not_called()


async def test_pod_di_produzione_senza_costi(hass, make_edist_coordinator, _db, _registro):
    coordinator = make_edist_coordinator(
        pods=[POD_A],
        options={CONF_TARIFFA: FASCE, CONF_TIPO_POD: {POD_A: TIPO_POD_PRODUZIONE}},
    )
    assert not coordinator.ha_costi(POD_A)
    assert await coordinator._async_aggiorna_costi(POD_A, _db) is None
    _registro.scrivi.assert_not_called()
