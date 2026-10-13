"""Visible texts that Home Assistant cannot translate via translations/*.json.

Entity names, config flow, actions and exceptions go through the translation
files and follow each user's profile language. A few strings stay out, which HA
treats as plain data with no translation key:

- the device model (DeviceInfo translates only the name);
- the external statistics name, stored in the Recorder metadata;
- the config flow description_placeholders, substituted as-is.

For these the server language is used (hass.config.language): Italian if it is
a variant of "it", otherwise English, like HA's fallback.

Pure module, no Home Assistant dependencies.
"""
from __future__ import annotations

LINGUA_PREDEFINITA = "en"

TESTI: dict[str, dict[str, str]] = {
    "it": {
        "modello_account": "Account API",
        "modello_prelievo": "Punto di prelievo",
        "modello_produzione": "Punto di produzione",
        "serie_prelievo": "prelievo",
        "serie_immissione": "immissione",
        "serie_produzione": "produzione",
        "serie_prelievo_tecnico": "prelievo (tecnico)",
        "serie_costo": "costo",
        "nota_reauth": " Stai aggiornando le credenziali per i POD: {pods}.",
        "nessun_pod": "nessuno",
        "nessun_dato_pod": (
            "E-Distribuzione non ha dati per questo periodo (giorni precedenti "
            "all'attivazione del contatore, o non ancora pubblicati)"
        ),
        "api_non_raggiungibile": (
            "il servizio dati di E-Distribuzione non è raggiungibile, controlla la "
            "connessione a internet"
        ),
        "api_non_autorizzata": (
            "E-Distribuzione ha rifiutato la richiesta dei dati (HTTP {stato}): il "
            "POD potrebbe non appartenere più all'account"
        ),
        "api_non_disponibile": (
            "il servizio dati di E-Distribuzione non risponde (HTTP {stato}), "
            "riprova più tardi"
        ),
        "api_rifiutata": "E-Distribuzione ha rifiutato la richiesta dei dati (HTTP {stato})",
        "api_errore": "E-Distribuzione ha risposto con un errore ({dettaglio})",
    },
    "en": {
        "modello_account": "API account",
        "modello_prelievo": "Withdrawal point",
        "modello_produzione": "Production point",
        "serie_prelievo": "consumption",
        "serie_immissione": "return to grid",
        "serie_produzione": "production",
        "serie_prelievo_tecnico": "consumption (technical)",
        "serie_costo": "cost",
        "nota_reauth": " You are updating the credentials for these PODs: {pods}.",
        "nessun_pod": "none",
        "nessun_dato_pod": (
            "E-Distribuzione has no data for this period (days before the meter "
            "was activated, or not published yet)"
        ),
        "api_non_raggiungibile": (
            "the E-Distribuzione data service cannot be reached, check the internet "
            "connection"
        ),
        "api_non_autorizzata": (
            "E-Distribuzione refused the data request (HTTP {stato}): the POD may no "
            "longer belong to the account"
        ),
        "api_non_disponibile": (
            "the E-Distribuzione data service is not responding (HTTP {stato}), try "
            "again later"
        ),
        "api_rifiutata": "E-Distribuzione refused the data request (HTTP {stato})",
        "api_errore": "E-Distribuzione answered with an error ({dettaglio})",
    },
}


def lingua_supportata(lingua: str | None) -> str:
    """'it', 'it-IT' -> 'it'; everything else (None included) -> English."""
    base = (lingua or "").split("-")[0].split("_")[0].lower()
    return base if base in TESTI else LINGUA_PREDEFINITA


def testo(lingua: str | None, chiave: str, **segnaposto: str) -> str:
    """Text for 'chiave' in the given language, with placeholders substituted."""
    valore = TESTI[lingua_supportata(lingua)][chiave]
    return valore.format(**segnaposto) if segnaposto else valore
