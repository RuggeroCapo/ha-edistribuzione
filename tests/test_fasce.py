"""Classification by ARERA time band (fasce.py).

Pure module: no 'hass' fixture, no Recorder. The samples are built as
raw_storage.leggi_campioni returns them, i.e. (utc_aware_interval_start, kwh).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.edistribuzione import fasce
from custom_components.edistribuzione.fasce import F1, F2, F3, FUSO_ITALIA


def _locale(iso: str) -> datetime:
    """Instant in Italian time, e.g. '2026-10-07 08:00'."""
    return datetime.fromisoformat(iso).replace(tzinfo=FUSO_ITALIA)


def _giornata(giorno: date, kwh_per_campione: float = 0.25) -> list[tuple[datetime, float]]:
    """All the 15-minute samples of a LOCAL day, in UTC: 96 normally, 92 or
    100 on the days of the clock change (as the API returns them)."""
    inizio = datetime(giorno.year, giorno.month, giorno.day, tzinfo=FUSO_ITALIA).astimezone(UTC)
    fine = datetime.combine(giorno + timedelta(days=1), datetime.min.time(), FUSO_ITALIA).astimezone(UTC)
    campioni = []
    ts = inizio
    while ts < fine:
        campioni.append((ts, kwh_per_campione))
        ts += timedelta(minutes=15)
    return campioni


def _totali(per_fascia: dict[str, list[tuple[datetime, float]]]) -> dict[str, float]:
    return {f: round(sum(kwh for _, kwh in ore), 6) for f, ore in per_fascia.items()}


# --- Easter and holidays ------------------------------------------------------

@pytest.mark.parametrize(
    ("anno", "attesa"),
    [
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
        (2038, date(2038, 4, 25)),  # the latest possible
        (2285, date(2285, 3, 22)),  # the earliest possible
    ],
)
def test_pasqua(anno, attesa):
    assert fasce.pasqua(anno) == attesa


def test_festivita_2026_comprende_pasquetta_e_le_fisse():
    giorni = fasce.festivita(2026)
    assert date(2026, 4, 6) in giorni  # Easter Monday
    for mese, giorno in ((1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15), (11, 1), (12, 8), (12, 25), (12, 26)):
        assert date(2026, mese, giorno) in giorni


def test_i_patroni_locali_non_sono_festivi():
    # Sant'Ambrogio (Milan), Tuesday 7/12/2027: a working day for ARERA.
    assert fasce.fascia(_locale("2027-12-07 10:00")) == F1


# --- Bands on a working day -------------------------------------------------

@pytest.mark.parametrize(
    ("ora", "attesa"),
    [
        ("00:00", F3),
        ("06:45", F3),
        ("07:00", F2),
        ("07:45", F2),
        ("08:00", F1),
        ("12:30", F1),
        ("18:45", F1),
        ("19:00", F2),
        ("22:45", F2),
        ("23:00", F3),
        ("23:45", F3),
    ],
)
def test_confini_feriale(ora, attesa):
    # Wednesday October 7, 2026
    assert fasce.fascia(_locale(f"2026-10-07 {ora}")) == attesa


@pytest.mark.parametrize(
    ("ora", "attesa"),
    [("06:45", F3), ("07:00", F2), ("12:00", F2), ("22:45", F2), ("23:00", F3)],
)
def test_confini_sabato(ora, attesa):
    assert fasce.fascia(_locale(f"2026-10-10 {ora}")) == attesa


@pytest.mark.parametrize("ora", ["03:00", "09:00", "12:00", "20:00"])
def test_domenica_sempre_f3(ora):
    assert fasce.fascia(_locale(f"2026-10-11 {ora}")) == F3


@pytest.mark.parametrize(
    "giorno",
    [
        "2026-04-06",  # Easter Monday
        "2026-06-02",  # Republic Day (Tuesday)
        "2026-12-08",  # Immaculate Conception (Tuesday)
    ],
)
def test_festivo_infrasettimanale_tutto_f3(giorno):
    for ora in ("07:30", "10:00", "20:00"):
        assert fasce.fascia(_locale(f"{giorno} {ora}")) == F3


def test_festivo_di_sabato_tutto_f3():
    # Saturday April 25, 2026: without the holiday it would be F2 from 7 to 23.
    assert fasce.fascia(_locale("2026-04-25 12:00")) == F3


# --- San Francesco (October 4) ---------------------------------------------

def test_san_francesco_2027_festivo_di_default():
    assert fasce.fascia(_locale("2027-10-04 10:00")) == F3


def test_san_francesco_disattivabile(monkeypatch):
    monkeypatch.setattr(fasce, "SAN_FRANCESCO_FESTIVO", False)
    assert fasce.fascia(_locale("2027-10-04 10:00")) == F1


def test_san_francesco_non_retroattivo():
    # Monday October 4, 2021: before Law 151/2025 it was a working day.
    assert fasce.fascia(_locale("2021-10-04 10:00")) == F1


# --- Time zone ----------------------------------------------------------------

def test_input_utc_convertito_in_ora_legale_estiva():
    # 06:00 UTC = 08:00 CEST
    assert fasce.fascia(datetime(2026, 10, 7, 6, 0, tzinfo=UTC)) == F1
    assert fasce.fascia(datetime(2026, 10, 7, 5, 45, tzinfo=UTC)) == F2


def test_input_utc_convertito_in_ora_solare():
    # 07:00 UTC = 08:00 CET
    assert fasce.fascia(datetime(2026, 12, 9, 7, 0, tzinfo=UTC)) == F1
    assert fasce.fascia(datetime(2026, 12, 9, 6, 45, tzinfo=UTC)) == F2


def test_datetime_naive_rifiutato():
    with pytest.raises(ValueError):
        fasce.fascia(datetime(2026, 10, 7, 10, 0))


# --- Hourly aggregation per band ---------------------------------------------

def test_giornata_feriale_intera():
    risultato = fasce.aggrega_ore_per_fascia(_giornata(date(2026, 10, 7)))
    # 1 kWh/hour: F1 = 8-19 (11 h), F2 = 7-8 + 19-23 (5 h), F3 = 0-7 + 23-24 (8 h)
    assert _totali(risultato) == {F1: 11.0, F2: 5.0, F3: 8.0}


def test_sabato_intero():
    risultato = fasce.aggrega_ore_per_fascia(_giornata(date(2026, 10, 10)))
    assert _totali(risultato) == {F1: 0.0, F2: 16.0, F3: 8.0}


def test_le_tre_serie_hanno_gli_stessi_timestamp_e_sommano_al_totale():
    campioni = _giornata(date(2026, 10, 9)) + _giornata(date(2026, 10, 10))  # Fri + Sat
    risultato = fasce.aggrega_ore_per_fascia(campioni)

    timestamp = [[ora for ora, _ in risultato[f]] for f in fasce.FASCE]
    assert timestamp[0] == timestamp[1] == timestamp[2]
    assert timestamp[0] == sorted(timestamp[0])
    assert len(timestamp[0]) == 48

    for i in range(48):
        assert sum(risultato[f][i][1] for f in fasce.FASCE) == pytest.approx(1.0)
        # every hour belongs to a single band
        assert sum(1 for f in fasce.FASCE if risultato[f][i][1] > 0) == 1


@pytest.mark.parametrize(
    ("giorno", "ore_attese"),
    [
        (date(2026, 3, 29), 23),   # start of daylight saving time
        (date(2026, 10, 25), 25),  # back to standard time
    ],
)
def test_cambio_ora_cade_di_domenica_tutto_f3(giorno, ore_attese):
    campioni = _giornata(giorno)
    assert len(campioni) == ore_attese * 4
    risultato = fasce.aggrega_ore_per_fascia(campioni)
    assert len(risultato[F3]) == ore_attese
    assert _totali(risultato) == {F1: 0.0, F2: 0.0, F3: float(ore_attese)}


def test_lista_vuota():
    assert fasce.aggrega_ore_per_fascia([]) == {F1: [], F2: [], F3: []}


class TestProssimoCambio:
    """Used by the current time band sensor to know when to update: the
    first instant after 'now' in which the band changes."""

    @pytest.mark.parametrize(
        ("adesso", "cambio", "fascia_dopo"),
        [
            # Monday: F2 -> F1 at 8, F1 -> F2 at 19, F2 -> F3 at 23.
            ("2026-10-12 07:30", "2026-10-12 08:00", F1),
            ("2026-10-12 08:00", "2026-10-12 19:00", F2),
            ("2026-10-12 22:59", "2026-10-12 23:00", F3),
            # Saturday evening: F3 until Monday at 7, the whole Sunday included.
            ("2026-10-17 23:00", "2026-10-19 07:00", F2),
            # Christmas 2026 is a Friday, Santo Stefano a Saturday: F3 from Thursday
            # at 23 to Monday 28 at 7.
            ("2026-12-24 23:30", "2026-12-28 07:00", F2),
        ],
    )
    def test_confini(self, adesso, cambio, fascia_dopo):
        risultato = fasce.prossimo_cambio(_locale(adesso))
        assert risultato == _locale(cambio)
        assert fasce.fascia(risultato) == fascia_dopo

    def test_attraversa_il_cambio_dell_ora(self):
        """Sunday March 28, 2027 is Easter and daylight saving time starts;
        Monday is Easter Monday, all F3. The boundary comes on Tuesday at 7
        Italian time, i.e. at 5 UTC instead of 6."""
        risultato = fasce.prossimo_cambio(_locale("2027-03-27 23:30"))
        assert risultato == _locale("2027-03-30 07:00")
        assert risultato.astimezone(UTC).hour == 5

    def test_risultato_in_utc(self):
        assert fasce.prossimo_cambio(_locale("2026-10-12 07:30")).tzinfo is UTC
