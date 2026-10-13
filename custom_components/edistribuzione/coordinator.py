"""DataUpdateCoordinator for E-Distribuzione.

Supports multiple PODs on the same config entry (same credential, same
authenticated account). Each cycle, for each POD, when it is time (queue +
hour, at most once a day): it requests BOTH energy directions (consumption and
injection, see MAGNITUDE_TUTTE in const.py) for the last GIORNI_RICONTROLLO
days (not only the most recent, to recheck any corrections E-Distribuzione made
to days already imported), in a single request per direction. Writing into the
external statistics always recomputes the whole series from the 15-minute raw
storage (see statistics.py), so a correction also fixes the following cumulative
sums automatically.

A day leaves the retry queue if at least one of the two directions returned it:
a POD without one of the two (e.g. a regular meter that never measures
injection) must not stay in the queue forever waiting for data that will not
arrive.

async_recupera_storico takes an optional 'pod' parameter: if omitted, it
fetches the history for ALL the PODs of the entry.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import costi, potenza, raw_storage
from .api import ApiClient, ApiError
from .auth import AccessoBloccato, AppBloccata, AuthClient, AuthError, TokenNonValido
from .const import (
    ABBANDONO_CODA_DOPO_GIORNI,
    CONF_DATA_INSTALLAZIONE,
    CONF_GIORNI_DA_RIPROVARE,
    CONF_ORA_RICHIESTA,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TARIFFA,
    CONF_TIPO_POD,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    GIORNI_RICONTROLLO,
    ISSUE_ACCESSO_BLOCCATO,
    ISSUE_APP_BLOCCATA,
    MAGNITUDE_IMMESSA,
    MAGNITUDE_PRELEVATA,
    MAGNITUDE_TUTTE,
    MAX_GIORNI_IN_CODA,
    MAX_GIORNI_RECUPERO_STORICO,
    ORA_MINIMA_RICHIESTA,
    RITARDO_DATI_GIORNI,
    TIPO_POD_DEFAULT,
    TIPO_POD_PRODUZIONE,
)
from .fasce import FUSO_ITALIA
from .statistics import (
    async_get_ultima_data_disponibile,
    async_import_curva_giornaliera,
    async_scrivi_costi,
    statistic_id_costo,
)
from .testi import testo

_LOGGER = logging.getLogger(__name__)


def _giorni_nel_periodo(data_da: date, data_a: date) -> list[date]:
    """List of days in the range, both ends included."""
    giorni, cursore = [], data_da
    while cursore <= data_a:
        giorni.append(cursore)
        cursore += timedelta(days=1)
    return giorni


def _giorni_ricevuti(curva: list[dict]) -> set[date]:
    """Days actually present in the response (sampleDate field, YYYYMMDD
    format), dropping those without samples."""
    giorni: set[date] = set()
    for elemento in curva:
        readings = elemento.get("readings", {})
        if not readings.get("sampleValues"):
            continue
        grezzo = readings.get("sampleDate")
        try:
            giorni.add(date(int(grezzo[:4]), int(grezzo[4:6]), int(grezzo[6:8])))
        except (TypeError, ValueError, IndexError):
            _LOGGER.warning("sampleDate non interpretabile, giorno ignorato: %r", grezzo)
    return giorni


def _curva_ha_dati(curva: list[dict]) -> bool:
    """True if the async_get_daily_load_profile response really contains
    samples, not just an empty structure."""
    return bool(curva) and bool(curva[0].get("readings", {}).get("sampleValues"))


def _magnitude_onorata(curva: list[dict], magnitude_richiesta: str) -> bool:
    """True if the server actually served the requested magnitude.

    'energyType' in the response echoes back the magnitude actually served: if
    the server silently ignores an unknown parameter and replies with
    consumption anyway, this field reveals it. Without this check, an ignored
    magnitude would look like a success and end up duplicating consumption into
    the injection series - the main risk of this integration until
    MAGNITUDE_IMMESSA is confirmed.

    An element without 'energyType' does not fail the check: it is assumed
    honoured rather than discarding good data for a missing field.
    """
    for elemento in curva:
        energy_type = elemento.get("readings", {}).get("energyType")
        if energy_type is not None and energy_type != magnitude_richiesta:
            return False
    return True


def _riepilogo_pod(
    ultima_data: date | None, ultimi_giorni: dict[str, tuple[date, float] | None]
) -> dict:
    """Values of a POD's diagnostic sensors.

    A direction without any sample, while the other one has some, belongs to a
    meter that never measured it (typically the return to grid without solar
    panels): it shows 0 kWh on the other direction's last day instead of
    staying unknown forever.
    """
    giorni_presenti = [giorno for giorno, _ in filter(None, ultimi_giorni.values())]
    per_direzione: dict[str, dict | None] = {}
    for direzione, valore in ultimi_giorni.items():
        if valore is None and giorni_presenti:
            valore = (max(giorni_presenti), 0.0)
        per_direzione[direzione] = (
            None if valore is None else {"giorno": valore[0].isoformat(), "kwh": round(valore[1], 3)}
        )
    return {
        "ultima_data_disponibile": ultima_data.isoformat() if ultima_data else None,
        "ultimo_giorno": per_direzione,
    }


def _descrivi_errore_api(err: ApiError, lingua: str) -> str:
    """One sentence on a data API error, for the messages shown to the user
    (in a placeholder, hence in the server language: see testi.py)."""
    if err.rete:
        return testo(lingua, "api_non_raggiungibile")
    if err.status in (401, 403):
        return testo(lingua, "api_non_autorizzata", stato=str(err.status))
    if err.status is not None and err.status >= 500:
        return testo(lingua, "api_non_disponibile", stato=str(err.status))
    if err.status is not None:
        return testo(lingua, "api_rifiutata", stato=str(err.status))
    return testo(lingua, "api_errore", dettaglio=str(err))


class EdistribuzioneCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({entry.title})",
            update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES),
            config_entry=entry,
        )
        self.entry = entry
        self.pods: list[str] = list(entry.data[CONF_PODS])
        # True while E-Distribuzione blocks the token refresh, with the anti-bot
        # check or by rejecting the OAuth client (see _async_ensure_token and
        # async_setup_entry).
        self.accesso_bloccato = False
        # What the diagnostic sensors show, per POD (see async_aggiorna_riepilogo).
        self.riepilogo: dict[str, dict] = {}
        # Per POD: (fingerprint of samples + tariff, cost summary). The cost
        # series is rewritten only when the fingerprint changes, not every hour.
        self._costi_calcolati: dict[str, tuple[tuple, dict | None]] = {}
        self._costi_puliti: set[str] = set()
        # Per POD: {"impegnata": kW, "disponibile": kW} from getSupplies, read
        # once a day (see _async_aggiorna_contratto).
        self.contratto: dict[str, dict[str, float | None]] = {}
        self._contratto_letto_il: date | None = None
        session = async_get_clientsession(hass)
        self._auth = AuthClient(session)
        self._api = ApiClient(session, access_token="")

    def tipo_pod(self, pod: str) -> str:
        """Role assigned by the user to the POD (exchange/production), from the
        config entry options. Only affects the visible labels (see
        _nome_serie), never which directions are requested."""
        tipi = self.entry.options.get(CONF_TIPO_POD, {})
        return tipi.get(pod, TIPO_POD_DEFAULT)

    @property
    def tariffa(self) -> costi.Tariffa | None:
        """Tariff set in the options (step "costi"), or None."""
        return costi.Tariffa.da_opzioni(self.entry.options.get(CONF_TARIFFA))

    def ha_costi(self, pod: str) -> bool:
        """Costs apply to the consumption of exchange PODs only: a production
        POD's consumption is the inverter's standby, not a bill."""
        return self.tariffa is not None and self.tipo_pod(pod) != TIPO_POD_PRODUZIONE

    @property
    def lingua(self) -> str:
        """Server language, for the texts HA does not translate (see testi.py)."""
        return self.hass.config.language

    def _nome_serie(self, pod: str, immessa: bool) -> str:
        """External statistic label per POD/direction, depending on the role
        chosen by the user. The name ends up in the Recorder metadata, with no
        translation: it follows the server language and updates on the first
        import after a language change."""
        if self.tipo_pod(pod) == TIPO_POD_PRODUZIONE:
            chiave = "serie_produzione" if immessa else "serie_prelievo_tecnico"
        else:
            chiave = "serie_immissione" if immessa else "serie_prelievo"
        return f"E-Distribuzione {pod} - {testo(self.lingua, chiave)}"

    def _segnala_blocco(self, issue: str) -> None:
        """Repairs notice for an access blocked by E-Distribuzione.

        The refresh_token stays valid and saved: we retry on the next cycle,
        and the notice explains why the data is stuck instead of leaving it to
        be inferred from the logs. Only one of the two notices at a time: they
        describe two different blocks.
        """
        self.accesso_bloccato = True
        for altro in (ISSUE_ACCESSO_BLOCCATO, ISSUE_APP_BLOCCATA):
            if altro != issue:
                ir.async_delete_issue(self.hass, DOMAIN, altro)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue,
        )

    async def _async_ensure_token(self) -> None:
        refresh_token = self.entry.data[CONF_REFRESH_TOKEN]
        try:
            tokens = await self._auth.async_refresh_access_token(refresh_token)
        except AccessoBloccato as err:
            self._segnala_blocco(ISSUE_ACCESSO_BLOCCATO)
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="accesso_bloccato"
            ) from err
        except AppBloccata as err:
            # Not a reauth: the login goes through the same OAuth client and is
            # refused too, while the saved refresh_token works again as soon as
            # E-Distribuzione lifts the block.
            _LOGGER.warning("Rinnovo dell'accesso rifiutato: %s", err)
            self._segnala_blocco(ISSUE_APP_BLOCCATA)
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="app_bloccata"
            ) from err
        except TokenNonValido as err:
            # The refresh_token was revoked or has expired (password change,
            # sessions closed on Enel's side, ...): Home Assistant shows the
            # reauth, which keeps PODs, options and history.
            _LOGGER.warning("Accesso a E-Distribuzione scaduto: %s", err)
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="accesso_scaduto"
            ) from err
        except AuthError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="refresh_token_fallito",
                translation_placeholders={"errore": str(err)},
            ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="portale_non_raggiungibile",
                translation_placeholders={"errore": type(err).__name__},
            ) from err

        # Access possible again: the notices, if any, are no longer needed.
        self.accesso_bloccato = False
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_ACCESSO_BLOCCATO)
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_APP_BLOCCATA)
        self._api.update_token(tokens.access_token)

        if tokens.refresh_token != refresh_token:
            new_data = dict(self.entry.data)
            new_data[CONF_REFRESH_TOKEN] = tokens.refresh_token
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    # ------------------------------------------------------------------
    # Diagnostic sensors
    # ------------------------------------------------------------------

    async def async_aggiorna_riepilogo(self) -> dict[str, dict]:
        """Refresh what the diagnostic sensors show, for every POD.

        Read from what has already been imported - the Recorder statistics for
        the last date, the local 15-minute database for the last day's kWh -
        not from the last request. Up to v0.6.0 the kWh came from the cycle
        that had just downloaded the data, so an hour later the sensors went
        back to unknown; now they stay right between one daily request and the
        next, after a restart, after a history fetch and while E-Distribuzione
        cannot be reached.
        """
        db_path = raw_storage.percorso_predefinito(self.hass)
        riepilogo: dict[str, dict] = {}
        for pod in self.pods:
            try:
                ultima_data = (
                    await async_get_ultima_data_disponibile(self.hass, pod)
                    if "recorder" in self.hass.config.components
                    else None
                )
                ultimi_giorni = {
                    direzione: await self.hass.async_add_executor_job(
                        raw_storage.kwh_ultimo_giorno, db_path, pod, direzione, FUSO_ITALIA
                    )
                    for direzione in MAGNITUDE_TUTTE
                }
            except Exception:  # noqa: BLE001 - diagnostics only, never fail an update
                _LOGGER.exception("POD %s: valori dei sensori diagnostici non aggiornati", pod)
                riepilogo[pod] = self.riepilogo.get(pod, {})
                continue
            riepilogo[pod] = _riepilogo_pod(ultima_data, ultimi_giorni)
            try:
                campioni_mese = await self.hass.async_add_executor_job(
                    raw_storage.campioni_mese_ultimo_giorno,
                    db_path,
                    pod,
                    MAGNITUDE_PRELEVATA,
                    FUSO_ITALIA,
                )
                disponibile = (self.contratto.get(pod) or {}).get("disponibile")
                riepilogo[pod]["potenza"] = potenza.riepilogo(campioni_mese, disponibile)
            except Exception:  # noqa: BLE001 - diagnostics only, never fail an update
                _LOGGER.exception("POD %s: picchi di potenza non aggiornati", pod)
                riepilogo[pod]["potenza"] = (self.riepilogo.get(pod) or {}).get("potenza")
            try:
                riepilogo[pod]["costi"] = await self._async_aggiorna_costi(pod, db_path)
            except Exception:  # noqa: BLE001 - costs are an extra, never fail an update
                _LOGGER.exception("POD %s: costi non aggiornati", pod)
                riepilogo[pod]["costi"] = (self.riepilogo.get(pod) or {}).get("costi")
        self.riepilogo = riepilogo
        return riepilogo

    async def _async_aggiorna_contratto(self) -> None:
        """Contract and available power of every POD, from getSupplies, at
        most once a day: they change only with a contract change. A failure
        keeps the previous values and is retried on the next cycle."""
        oggi = dt_util.now().date()
        if self._contratto_letto_il == oggi:
            return
        try:
            forniture = await self._api.async_get_supplies()
        except Exception:  # noqa: BLE001 - an extra, never fail an update
            _LOGGER.warning("Potenza contrattuale non letta da getSupplies", exc_info=True)
            return

        def _kw(valore) -> float | None:
            try:
                return float(valore) if valore is not None else None
            except (TypeError, ValueError):
                return None

        for fornitura in forniture:
            if fornitura.get("IdPod") in self.pods:
                self.contratto[fornitura["IdPod"]] = {
                    "impegnata": _kw(fornitura.get("ContractualPower")),
                    "disponibile": _kw(fornitura.get("AvailablePower")),
                }
        self._contratto_letto_il = oggi

    async def _async_aggiorna_costi(self, pod: str, db_path: str) -> dict | None:
        """Rewrite the POD's cost series when samples or tariff changed, and
        return the summary shown by the cost sensors (None without a tariff).

        Without a tariff, a cost series left by an earlier configuration is
        removed once, together with its link in the Energy Dashboard."""
        from .energy_dashboard import async_collega_costo

        recorder = "recorder" in self.hass.config.components
        tariffa = self.tariffa if self.ha_costi(pod) else None
        if tariffa is None:
            if self._costi_calcolati.pop(pod, None) is not None or pod not in self._costi_puliti:
                self._costi_puliti.add(pod)
                if recorder:
                    await async_collega_costo(self.hass, pod, attivo=False)
                    get_instance(self.hass).async_clear_statistics([statistic_id_costo(pod)])
            return None

        campioni = await self.hass.async_add_executor_job(
            raw_storage.leggi_campioni, db_path, pod, MAGNITUDE_PRELEVATA
        )
        impronta = (
            tariffa,
            len(campioni),
            campioni[-1][0] if campioni else None,
            round(sum(kwh for _, kwh in campioni), 6),
        )
        precedente = self._costi_calcolati.get(pod)
        if precedente is not None and precedente[0] == impronta:
            return precedente[1]

        ore = await self.hass.async_add_executor_job(costi.costi_orari, campioni, tariffa)
        if ore and recorder:
            async_scrivi_costi(
                self.hass, pod, ore, f"E-Distribuzione {pod} - {testo(self.lingua, 'serie_costo')}"
            )
            await async_collega_costo(self.hass, pod, attivo=True)
        self._costi_puliti.discard(pod)
        sintesi = costi.riepilogo(ore)
        self._costi_calcolati[pod] = (impronta, sintesi)
        return sintesi

    # ------------------------------------------------------------------
    # Configurable hour + queue of days to retry, PER POD
    # ------------------------------------------------------------------

    @property
    def _ora_richiesta(self) -> int:
        """Hour (local) from which to request the previous day's curve.
        Configurable from the options: if requests often come back empty it is
        worth moving it later."""
        valore = self.entry.options.get(CONF_ORA_RICHIESTA)
        if valore is None:
            return ORA_MINIMA_RICHIESTA
        try:
            ora = int(float(valore))
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Ora richiesta non valida nelle opzioni (%r): uso le %d:00",
                valore,
                ORA_MINIMA_RICHIESTA,
            )
            return ORA_MINIMA_RICHIESTA
        if not 0 <= ora <= 23:
            _LOGGER.warning(
                "Ora richiesta fuori intervallo (%r): uso le %d:00", valore, ORA_MINIMA_RICHIESTA
            )
            return ORA_MINIMA_RICHIESTA
        return ora

    def _leggi_code(self) -> dict[str, dict[str, date]]:
        """Queues of days to retry, ONE PER POD:
        {pod: {ISO day: date first queued}}.

        The first-queued date acts as a timer: a day is dropped after
        ABBANDONO_CODA_DOPO_GIORNI regardless of the number of attempts (see
        _scrivi_code).
        """
        grezzo = self.entry.data.get(CONF_GIORNI_DA_RIPROVARE) or {}
        oggi = dt_util.now().date()

        def _con_date(coda: dict) -> dict[str, date]:
            risultato: dict[str, date] = {}
            for giorno, valore in coda.items():
                try:
                    risultato[giorno] = date.fromisoformat(valore)
                except (TypeError, ValueError):
                    risultato[giorno] = oggi
            return risultato

        return {pod: _con_date(coda) for pod, coda in grezzo.items()}

    def _scrivi_code(self, code: dict[str, dict[str, date]]) -> None:
        """Save the queues, dropping days that are too old and capping their number, per POD."""
        oggi = dt_util.now().date()
        pulite: dict[str, dict[str, str]] = {}
        for pod, coda in code.items():
            pulita = {
                giorno: da
                for giorno, da in coda.items()
                if (oggi - da).days < ABBANDONO_CODA_DOPO_GIORNI
            }
            abbandonati = set(coda) - set(pulita)
            if abbandonati:
                _LOGGER.warning(
                    "POD %s: giorni abbandonati dopo %d giorni in coda senza dati da "
                    "E-Distribuzione: %s. Se servono, richiedili con l'azione "
                    "edistribuzione.recupera_storico.",
                    pod,
                    ABBANDONO_CODA_DOPO_GIORNI,
                    ", ".join(sorted(abbandonati)),
                )

            if len(pulita) > MAX_GIORNI_IN_CODA:
                tenuti = sorted(pulita, reverse=True)[:MAX_GIORNI_IN_CODA]
                scartati = set(pulita) - set(tenuti)
                _LOGGER.warning(
                    "POD %s: coda dei giorni da riprovare oltre %d elementi: scarto i "
                    "più vecchi (%s)",
                    pod,
                    MAX_GIORNI_IN_CODA,
                    ", ".join(sorted(scartati)),
                )
                pulita = {g: pulita[g] for g in tenuti}

            if pulita:
                pulite[pod] = {g: da.isoformat() for g, da in pulita.items()}

        if pulite != self.entry.data.get(CONF_GIORNI_DA_RIPROVARE):
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_GIORNI_DA_RIPROVARE: pulite},
            )

    def _accoda_giorno(self, pod: str, giorno: date) -> None:
        code = self._leggi_code()
        coda = code.setdefault(pod, {})
        chiave = giorno.isoformat()
        if chiave in coda:
            _LOGGER.info(
                "POD %s: giorno %s ancora senza dati da E-Distribuzione, in coda da "
                "%d giorni (max %d)",
                pod,
                chiave,
                (dt_util.now().date() - coda[chiave]).days,
                ABBANDONO_CODA_DOPO_GIORNI,
            )
        else:
            coda[chiave] = dt_util.now().date()
            _LOGGER.info(
                "POD %s: giorno %s senza dati da E-Distribuzione, messo in coda per "
                "riprovare (max %d giorni)",
                pod,
                chiave,
                ABBANDONO_CODA_DOPO_GIORNI,
            )
        self._scrivi_code(code)

    def _rimuovi_dalla_coda(self, pod: str, giorni: list[date]) -> None:
        code = self._leggi_code()
        coda = code.get(pod, {})
        rimossi = [g.isoformat() for g in giorni if g.isoformat() in coda]
        if not rimossi:
            return
        for chiave in rimossi:
            del coda[chiave]
        _LOGGER.info("POD %s: dati ricevuti per %s, rimossi dalla coda", pod, ", ".join(rimossi))
        self._scrivi_code(code)

    async def _prossima_richiesta(self, pod: str) -> tuple[date, date] | None:
        """Decide which range to request for this POD in this cycle.

        A POD with NO data imported yet (the entry's first start, or a POD added
        later from the options) requests immediately, regardless of the
        configured hour - this checks right away that the POD and token are
        valid, instead of finding out only in the evening. On later cycles it
        waits for the configured hour, then at most once a day (the same
        'atteso' stays unchanged until the day changes) requests in a SINGLE
        request the last GIORNI_RICONTROLLO days up to the expected day - not
        only the most recent: E-Distribuzione may correct a day already
        published, and without this periodic recheck that correction would never
        be seen automatically (it stays recoverable by hand with
        recupera_storico).

        If there are backlog days OLDER than the recheck window (stuck in the
        queue from a previous error), the range widens backwards to include
        them, still in a single request.

        PAST BUG (fixed here): the previous version made the immediate fetch
        depend on CONF_DATA_INSTALLAZIONE, a flag SHARED by the whole config
        entry instead of per-POD. With several PODs configured from the first
        start, only the first in the list was checked immediately - the second
        (and later) ones stayed without any data until the configured hour,
        because by the time the cycle reached them the flag had already been set
        by the first. The correct condition is "does this POD already have
        data?", not "has the entry's first start already happened?".
        """
        oggi = dt_util.now().date()
        atteso = oggi - timedelta(days=RITARDO_DATI_GIORNI)

        if not self.entry.data.get(CONF_DATA_INSTALLAZIONE):
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_DATA_INSTALLAZIONE: oggi.isoformat()},
            )

        ultima_disponibile = await async_get_ultima_data_disponibile(self.hass, pod)

        if ultima_disponibile is None:
            _LOGGER.info(
                "POD %s: nessun dato ancora importato, richiedo subito gli ultimi %d "
                "giorni per verificare POD e token. Le richieste successive partiranno "
                "dopo le %d:00 (modificabile dalle opzioni); per lo storico usa l'azione "
                "edistribuzione.recupera_storico.",
                pod,
                GIORNI_RICONTROLLO,
                self._ora_richiesta,
            )
        else:
            adesso = dt_util.now()
            if adesso.hour < self._ora_richiesta:
                return None
            if ultima_disponibile >= atteso:
                # Already rechecked today ('atteso' stays the same until
                # midnight): at most one request per day per POD, not one on
                # every hourly cycle after the configured hour.
                return None

        inizio_ricontrollo = atteso - timedelta(days=GIORNI_RICONTROLLO - 1)

        code = self._leggi_code()
        coda = code.get(pod, {})
        arretrati = sorted(
            date.fromisoformat(g) for g in coda if date.fromisoformat(g) < inizio_ricontrollo
        )
        inizio = (
            max(arretrati[0], atteso - timedelta(days=150)) if arretrati else inizio_ricontrollo
        )

        return inizio, atteso

    # ------------------------------------------------------------------
    # Fetch + import, shared between the automatic cycle and the history fetch
    # ------------------------------------------------------------------

    async def _async_importa_periodo(self, pod: str, data_da: date, data_a: date) -> dict[str, dict]:
        """Request and import BOTH directions for a POD over the period
        [data_da, data_a] (both ends included), in two separate API calls.

        A single request per direction, not one per day: the endpoint accepts a
        real multi-day range in a single response (up to 181 days).

        Returns {magnitude: {"giorni_ricevuti": set[date]}} for the directions
        that returned valid, honoured data (see _magnitude_onorata) - a
        direction with no data for this POD/period simply does not appear in
        the result.
        """
        risultati: dict[str, dict] = {}
        for magnitude in MAGNITUDE_TUTTE:
            curva = await self._api.async_get_daily_load_profile(
                pod, data_da, data_a, magnitude=magnitude
            )
            if not _curva_ha_dati(curva):
                continue
            if not _magnitude_onorata(curva, magnitude):
                _LOGGER.warning(
                    "POD %s: risposta per magnitude=%r con energyType diverso da quello "
                    "richiesto (probabile parametro ignorato dal server): scartata, non "
                    "importata per evitare di duplicare un'altra direzione.",
                    pod,
                    magnitude,
                )
                continue

            immessa = magnitude == MAGNITUDE_IMMESSA
            await async_import_curva_giornaliera(
                self.hass, pod, curva, immessa=immessa, nome=self._nome_serie(pod, immessa)
            )

            risultati[magnitude] = {"giorni_ricevuti": _giorni_ricevuti(curva)}
        return risultati

    # ------------------------------------------------------------------
    # Automatic polling cycle
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict:
        await self._async_ensure_token()
        await self._async_aggiorna_contratto()

        for pod in self.pods:
            richiesta = await self._prossima_richiesta(pod)

            if richiesta is not None:
                data_da, data_a = richiesta
                try:
                    risultati = await self._async_importa_periodo(pod, data_da, data_a)
                except ApiError as err:
                    # The requested days go into the queue instead of being
                    # lost: on the next cycle 'atteso' would already have moved.
                    for giorno in _giorni_nel_periodo(data_da, data_a):
                        self._accoda_giorno(pod, giorno)
                    raise UpdateFailed(
                        translation_domain=DOMAIN,
                        translation_key="errore_import_pod",
                        translation_placeholders={
                            "pod": pod,
                            "errore": _descrivi_errore_api(err, self.lingua),
                        },
                    ) from err

                ricevuti_prelevata = risultati.get(MAGNITUDE_PRELEVATA, {}).get(
                    "giorni_ricevuti", set()
                )
                ricevuti_immessa = risultati.get(MAGNITUDE_IMMESSA, {}).get(
                    "giorni_ricevuti", set()
                )
                ricevuti_unione = ricevuti_prelevata | ricevuti_immessa

                # A day leaves the queue if AT LEAST ONE direction returned it:
                # a POD without one of the two must not stay in the queue
                # forever waiting for data that will not arrive.
                richiesti = _giorni_nel_periodo(data_da, data_a)
                if ricevuti_unione:
                    self._rimuovi_dalla_coda(pod, [g for g in richiesti if g in ricevuti_unione])
                for giorno in richiesti:
                    if giorno not in ricevuti_unione:
                        self._accoda_giorno(pod, giorno)

                if ricevuti_prelevata != ricevuti_immessa:
                    _LOGGER.info(
                        "POD %s: prelevata e immessa non coprono esattamente gli stessi "
                        "giorni in questo ciclo (prelevata=%s, immessa=%s) - possibile "
                        "sfasamento nella pubblicazione dei due registri.",
                        pod,
                        sorted(g.isoformat() for g in ricevuti_prelevata),
                        sorted(g.isoformat() for g in ricevuti_immessa),
                    )

        return {"by_pod": await self.async_aggiorna_riepilogo()}

    # ------------------------------------------------------------------
    # Manual history fetch (edistribuzione.recupera_storico action)
    # ------------------------------------------------------------------

    async def async_recupera_storico(
        self, data_da: date, data_a: date, pod: str | None = None
    ) -> None:
        """Fetch and import both directions for the range [data_da, data_a].

        If 'pod' is omitted, it does so for ALL the PODs configured on the
        entry; if specified, only for that one.

        Raises HomeAssistantError if nothing was imported for any POD (neither
        consumption nor injection): the action is manual and launched from the
        UI, where a silent failure is indistinguishable from a success. With
        several PODs and only a partial failure the action succeeds - something
        was imported - and the failed PODs stay in the logs.

        Every error message says what to change: the action is often written by
        hand in YAML, where swapped or too recent dates are easy to get.
        """
        if pod is not None and pod not in self.pods:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="pod_non_configurato",
                translation_placeholders={"pod": pod, "pods": ", ".join(self.pods)},
            )
        pod_da_recuperare = [pod] if pod else list(self.pods)

        self._verifica_periodo(data_da, data_a)

        try:
            await self._async_ensure_token()
        except ConfigEntryAuthFailed:
            # Outside the polling cycle Home Assistant does not start the
            # reauth on its own: without it, the message would point to a
            # button that is not there.
            self.entry.async_start_reauth(self.hass)
            raise

        _LOGGER.info(
            "Recupero storico avviato: %s - %s (POD: %s)",
            data_da,
            data_a,
            ", ".join(pod_da_recuperare),
        )

        fallimenti: list[str] = []
        pod_con_dati = 0

        for pod_corrente in pod_da_recuperare:
            try:
                risultati = await self._async_importa_periodo(pod_corrente, data_da, data_a)
            except ApiError as err:
                _LOGGER.warning(
                    "POD %s: errore recuperando il periodo %s - %s: %s",
                    pod_corrente,
                    data_da,
                    data_a,
                    err,
                )
                fallimenti.append(f"{pod_corrente}: {_descrivi_errore_api(err, self.lingua)}")
                continue

            ricevuti_unione: set[date] = set()
            for info in risultati.values():
                ricevuti_unione |= info["giorni_ricevuti"]

            if not ricevuti_unione:
                _LOGGER.warning(
                    "POD %s: nessun dato (né prelevata né immessa) per il periodo %s - %s",
                    pod_corrente,
                    data_da,
                    data_a,
                )
                fallimenti.append(f"{pod_corrente}: {testo(self.lingua, 'nessun_dato_pod')}")
                continue

            giorni_attesi = _giorni_nel_periodo(data_da, data_a)
            self._rimuovi_dalla_coda(
                pod_corrente, [g for g in giorni_attesi if g in ricevuti_unione]
            )
            pod_con_dati += 1

            mancanti = [g for g in giorni_attesi if g not in ricevuti_unione]
            dettaglio_mancanti = ""
            if mancanti:
                elenco = ", ".join(g.isoformat() for g in mancanti[:10])
                if len(mancanti) > 10:
                    elenco += f", e altri {len(mancanti) - 10}"
                dettaglio_mancanti = f" ({elenco})"

            _LOGGER.info(
                "POD %s: recupero storico %s - %s completato, %d/%d giorni ricevuti "
                "(unione delle due direzioni)%s",
                pod_corrente,
                data_da,
                data_a,
                len(ricevuti_unione),
                len(giorni_attesi),
                dettaglio_mancanti,
            )

        if pod_con_dati == 0:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="nessun_dato_importato",
                translation_placeholders={
                    "data_da": str(data_da),
                    "data_a": str(data_a),
                    "dettagli": "; ".join(fallimenti),
                },
            )

        # The sensors and the card read what was just imported right away,
        # without waiting for the next hourly cycle.
        await self.async_aggiorna_riepilogo()
        self.async_update_listeners()

    @staticmethod
    def _verifica_periodo(data_da: date, data_a: date) -> None:
        """Reject a range that cannot give data, saying how to fix it.

        Checked in this order because the first problem found is the one to
        fix: swapped dates first (both "too recent" errors would be misleading
        then), then a start or end E-Distribuzione has not published yet, then
        the length.
        """
        ultimo_utile = dt_util.now().date() - timedelta(days=RITARDO_DATI_GIORNI)

        def errore(chiave: str, **segnaposto: str) -> ServiceValidationError:
            return ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=chiave,
                translation_placeholders={
                    "data_da": data_da.isoformat(),
                    "data_a": data_a.isoformat(),
                    "ultimo_utile": ultimo_utile.isoformat(),
                    **segnaposto,
                },
            )

        if data_da > data_a:
            raise errore("date_invertite")
        if data_da > ultimo_utile:
            raise errore("data_inizio_troppo_recente")
        if data_a > ultimo_utile:
            raise errore("data_fine_troppo_recente")

        giorni_totali = (data_a - data_da).days + 1
        if giorni_totali > MAX_GIORNI_RECUPERO_STORICO:
            fine_prima_parte = data_da + timedelta(days=MAX_GIORNI_RECUPERO_STORICO - 1)
            raise errore(
                "intervallo_troppo_ampio",
                giorni=str(giorni_totali),
                massimo=str(MAX_GIORNI_RECUPERO_STORICO),
                fine_prima_parte=fine_prima_parte.isoformat(),
            )

    @property
    def api(self) -> ApiClient:
        """Expose the API client for on-demand calls."""
        return self._api
