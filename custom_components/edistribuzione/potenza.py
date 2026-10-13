"""Power drawn from the grid, from the 15-minute energy samples.

A sample is the kWh of a quarter of an hour, so its average power is
kwh * 4 kW. That is the 15-minute average, the quantity the meter itself uses
for its monthly peaks: instantaneous spikes (a kettle starting) are higher and
are not in this data. Enough to see how close the house gets to the contract
power (ContractualPower from getSupplies, e.g. 3 kW, with AvailablePower 10%
above, e.g. 3.3 kW), not to predict every trip of the limiter.

Pure module, no Home Assistant dependencies: testable with plain pytest.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from . import fasce

QUARTI_ORA_PER_ORA = 4


def kw(kwh_quarto_ora: float) -> float:
    """Average kW over a 15-minute sample."""
    return kwh_quarto_ora * QUARTI_ORA_PER_ORA


def ore_potenza(
    campioni: list[tuple[datetime, float]],
) -> list[tuple[datetime, float, float, float]]:
    """(hour start UTC, mean kW, min kW, max kW) of the 15-minute averages in
    each hour - the rows of a mean/min/max statistic, so a statistics graph
    can show the hourly peaks."""
    per_ora: dict[datetime, list[float]] = defaultdict(list)
    for ts, kwh in campioni:
        per_ora[ts.replace(minute=0, second=0, microsecond=0)].append(kw(kwh))
    return [
        (ora, sum(valori) / len(valori), min(valori), max(valori))
        for ora, valori in sorted(per_ora.items())
    ]


def _picco(campioni: list[tuple[datetime, float]]) -> dict | None:
    if not campioni:
        return None
    ts, kwh = max(campioni, key=lambda c: c[1])
    return {
        "kw": round(kw(kwh), 2),
        "quando": ts.astimezone(fasce.FUSO_ITALIA).isoformat(),
        "fascia": fasce.fascia(ts).upper(),
    }


def riepilogo(campioni_mese: list[tuple[datetime, float]], disponibile_kw: float | None) -> dict | None:
    """Peak of the last day with data and of its month up to that day.

    'campioni_mese' are the samples of the month of the last day (see
    raw_storage.campioni_mese_ultimo_giorno). With the available power known,
    each peak also carries its share of it in percent."""
    if not campioni_mese:
        return None
    ultimo_giorno = campioni_mese[-1][0].astimezone(fasce.FUSO_ITALIA).date()
    del_giorno = [
        c for c in campioni_mese if c[0].astimezone(fasce.FUSO_ITALIA).date() == ultimo_giorno
    ]
    risultato = {
        "giorno": ultimo_giorno.isoformat(),
        "mese": ultimo_giorno.strftime("%Y-%m"),
        "picco_giorno": _picco(del_giorno),
        "picco_mese": _picco(campioni_mese),
    }
    if disponibile_kw:
        for chiave in ("picco_giorno", "picco_mese"):
            picco = risultato[chiave]
            picco["percentuale_disponibile"] = round(picco["kw"] / disponibile_kw * 100)
    return risultato
