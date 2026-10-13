"""Cost of the consumption, from the 15-minute samples and the user's tariff.

The tariff is set in the integration options (step "costi") and stored in
entry.options[CONF_TARIFFA] as a plain dict; Tariffa.da_opzioni turns it back
into this dataclass. Two kinds of fixed-price offer:

    monoraria   one energy price for every hour
    fasce       one price per ARERA band (F1/F2/F3, see fasce.py)

On top of the energy price, the parts of an Italian bill that scale the same
way are entered once and applied as they are:

    extra_kwh           EUR/kWh not included in the offer price (transport,
                        system charges, excise): one figure, read off the bill
    quota_fissa_mese    EUR/month (commercial fee, fixed transport share)
    potenza_kw          committed power (kW) ...
    quota_potenza       ... times EUR/kW/month
    iva                 VAT percent, applied to everything

Fixed monthly amounts are spread evenly over the hours of that (Italian) month,
so a month with the clock change still adds up to exactly one monthly fee and
the hourly cost series sums to the bill estimate.

The same tariff is applied to the whole history: changing a price recomputes
the past too.

Pure module, no Home Assistant dependencies: testable with plain pytest.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from . import fasce

TIPO_MONORARIA = "monoraria"
TIPO_FASCE = "fasce"
TIPI_TARIFFA = (TIPO_MONORARIA, TIPO_FASCE)


@dataclass(frozen=True)
class Tariffa:
    tipo: str
    prezzo_f1: float
    prezzo_f2: float
    prezzo_f3: float
    extra_kwh: float = 0.0
    quota_fissa_mese: float = 0.0
    potenza_kw: float = 0.0
    quota_potenza: float = 0.0
    iva: float = 10.0

    @classmethod
    def da_opzioni(cls, opzioni: dict | None) -> Tariffa | None:
        """Tariff from the options dict, or None if not configured."""
        if not opzioni or opzioni.get("tipo") not in TIPI_TARIFFA:
            return None
        tipo = opzioni["tipo"]
        if tipo == TIPO_MONORARIA:
            prezzo = float(opzioni["prezzo"])
            prezzi = (prezzo, prezzo, prezzo)
        else:
            prezzi = tuple(float(opzioni[f"prezzo_{f}"]) for f in fasce.FASCE)
        return cls(
            tipo,
            *prezzi,
            extra_kwh=float(opzioni.get("extra_kwh", 0.0)),
            quota_fissa_mese=float(opzioni.get("quota_fissa_mese", 0.0)),
            potenza_kw=float(opzioni.get("potenza_kw", 0.0)),
            quota_potenza=float(opzioni.get("quota_potenza", 0.0)),
            iva=float(opzioni.get("iva", 10.0)),
        )

    def prezzo_kwh(self, istante: datetime) -> float:
        """EUR/kWh before VAT for a sample starting at 'istante'."""
        prezzo = {fasce.F1: self.prezzo_f1, fasce.F2: self.prezzo_f2, fasce.F3: self.prezzo_f3}[
            fasce.fascia(istante)
        ]
        return prezzo + self.extra_kwh

    @property
    def fisso_mese(self) -> float:
        """Fixed EUR/month before VAT."""
        return self.quota_fissa_mese + self.potenza_kw * self.quota_potenza

    @property
    def moltiplicatore_iva(self) -> float:
        return 1 + self.iva / 100


def _inizio_mese(giorno: date) -> datetime:
    return datetime.combine(giorno.replace(day=1), time.min, tzinfo=fasce.FUSO_ITALIA)


def ore_nel_mese(istante: datetime) -> int:
    """Hours of the Italian month 'istante' falls in: 743 or 745 in the months
    of the clock change."""
    giorno = istante.astimezone(fasce.FUSO_ITALIA).date()
    inizio = _inizio_mese(giorno)
    fine = _inizio_mese((inizio + timedelta(days=32)).date())
    return round((fine.astimezone(UTC) - inizio.astimezone(UTC)).total_seconds() / 3600)


def costi_orari(
    campioni: list[tuple[datetime, float]], tariffa: Tariffa
) -> list[tuple[datetime, float]]:
    """(hour start UTC, EUR) for every hour with at least one sample - the
    same timestamps as the hourly energy series. VAT included."""
    energia: dict[datetime, float] = defaultdict(float)
    for ts, kwh in campioni:
        energia[ts.replace(minute=0, second=0, microsecond=0)] += kwh * tariffa.prezzo_kwh(ts)

    fisso = tariffa.fisso_mese
    return [
        (ora, (euro + fisso / ore_nel_mese(ora)) * tariffa.moltiplicatore_iva)
        for ora, euro in sorted(energia.items())
    ]


def riepilogo(ore: list[tuple[datetime, float]]) -> dict | None:
    """Cost of the last day with data and of its month up to that day, as
    shown by the sensors. None if there is no data."""
    if not ore:
        return None
    ultimo_giorno = ore[-1][0].astimezone(fasce.FUSO_ITALIA).date()
    costo_giorno = costo_mese = 0.0
    for ora, euro in ore:
        giorno = ora.astimezone(fasce.FUSO_ITALIA).date()
        if giorno == ultimo_giorno:
            costo_giorno += euro
        if (giorno.year, giorno.month) == (ultimo_giorno.year, ultimo_giorno.month):
            costo_mese += euro
    return {
        "giorno": ultimo_giorno.isoformat(),
        "costo_giorno": round(costo_giorno, 2),
        "mese": ultimo_giorno.strftime("%Y-%m"),
        "costo_mese": round(costo_mese, 2),
    }
