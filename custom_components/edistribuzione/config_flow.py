"""edistribuzione config flow.

Setup flow: user (open the authorization link in your own browser, log in
there, paste back the eneldist://redirect URL) -> pod (multi-select among the
account's PODs). Reauth: the same user step (told apart by an internal branch
on self._reauth_entry), without going back to choosing the PODs - only the
existing entry's refresh_token is updated.

Home Assistant never receives the password or the OTP: see auth.py.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiClient
from .auth import (
    AccessoBloccato,
    AppBloccata,
    AuthClient,
    AutorizzazioneNegata,
    ParsingError,
    StateNonCorrispondente,
    TokenNonValido,
    UrlRedirectNonValido,
)
from .const import (
    CONF_ORA_RICHIESTA,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    DOMAIN,
    ORA_MINIMA_RICHIESTA,
    TIPO_POD_DEFAULT,
    TIPO_POD_PRODUZIONE,
    TIPO_POD_SCAMBIO,
)
from .testi import testo

_LOGGER = logging.getLogger(__name__)

CONF_URL_REDIRECT = "url_redirect"

STEP_USER_SCHEMA = vol.Schema({vol.Required(CONF_URL_REDIRECT): str})


def _descrivi_eccezione(err: Exception) -> str:
    """Short technical detail for the "unexpected error" messages."""
    testo = str(err).strip()
    return f"{type(err).__name__}: {testo}"[:200] if testo else type(err).__name__


def _etichetta_pod(pod_info: dict) -> str:
    """'IT001E12345678 - Via Roma 1, Milano (MI)' instead of just the POD code
    in the selector, so you can tell at a glance which property it is without
    looking elsewhere."""
    indirizzo = (
        f"{pod_info.get('PointOfMeasureStreetPrefix', '')} "
        f"{pod_info.get('PointOfMeasureStreet', '')} "
        f"{pod_info.get('PointOfMeasureStreetNumber', '')}, "
        f"{pod_info.get('PointOfMeasureMunicipality', '')} "
        f"({pod_info.get('PointOfMeasureProvince', '')})"
    ).strip()
    return f"{pod_info['IdPod']} - {indirizzo}"


class EdistribuzioneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for the edistribuzione integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._auth: AuthClient | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._pods_disponibili: list[dict] = []
        self._reauth_entry: config_entries.ConfigEntry | None = None

    def _testo(self, chiave: str, **segnaposto: str) -> str:
        """Text for the description_placeholders, which HA does not translate: see testi.py."""
        return testo(self.hass.config.language, chiave, **segnaposto)

    # ------------------------------------------------------------------
    # Login in the browser -> pasted redirect URL
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        messaggio = ""

        # One AuthClient (hence one PKCE pair and one link) for the whole
        # flow: a failed paste does not invalidate the link already opened.
        if self._auth is None:
            self._auth = AuthClient(async_get_clientsession(self.hass))
        link = self._auth.crea_link_autorizzazione()

        if user_input is not None:
            try:
                tokens = await self._auth.async_scambia_url_redirect(
                    user_input[CONF_URL_REDIRECT]
                )
            except UrlRedirectNonValido:
                errors["base"] = "url_non_valido"
            except StateNonCorrispondente:
                errors["base"] = "state_diverso"
            except AutorizzazioneNegata as err:
                errors["base"] = "autorizzazione_negata"
                messaggio = str(err)
            except TokenNonValido:
                # The code is single-use and lasts a few minutes: a new login
                # with the same link gives a fresh one.
                errors["base"] = "codice_scaduto"
            except AccessoBloccato:
                _LOGGER.warning("Scambio del codice: E-Distribuzione ha risposto con la verifica antibot")
                errors["base"] = "accesso_bloccato"
            except AppBloccata as err:
                _LOGGER.warning("Scambio del codice: E-Distribuzione blocca il client OAuth (%s)", err)
                errors["base"] = "app_bloccata"
            except ParsingError as err:
                _LOGGER.exception("Risposta inattesa dal token endpoint")
                errors["base"] = "risposta_inattesa"
                messaggio = _descrivi_eccezione(err)
            except (aiohttp.ClientError, TimeoutError):
                _LOGGER.exception("E-Distribuzione non raggiungibile durante lo scambio del codice")
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001 - any other unexpected error
                # Better an error in the form (with the traceback in the logs)
                # than blowing up the step.
                _LOGGER.exception("Errore imprevisto durante lo scambio del codice")
                errors["base"] = "errore_imprevisto"
                messaggio = _descrivi_eccezione(err)
            else:
                self._access_token = tokens.access_token
                self._refresh_token = tokens.refresh_token
                if self._reauth_entry is not None:
                    nuovi_dati = {
                        **self._reauth_entry.data,
                        CONF_REFRESH_TOKEN: tokens.refresh_token,
                    }
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry, data=nuovi_dati
                    )
                    await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                    return self.async_abort(reason="reauth_successful")
                return await self.async_step_pod()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={
                "link": link,
                "pod_correnti": self._nota_reauth(),
                "messaggio": messaggio,
            },
        )

    def _nota_reauth(self) -> str:
        """Extra sentence shown only during a reauth, to recall which PODs will
        be re-authenticated - empty during the initial setup."""
        if self._reauth_entry is None:
            return ""
        pods = ", ".join(self._reauth_entry.data.get(CONF_PODS, []))
        return self._testo("nota_reauth", pods=pods)

    # ------------------------------------------------------------------
    # POD selection (initial setup only, not reauth)
    # ------------------------------------------------------------------

    async def async_step_pod(self, user_input: dict[str, Any] | None = None):
        session = async_get_clientsession(self.hass)
        api = ApiClient(session, self._access_token)

        if not self._pods_disponibili:
            try:
                self._pods_disponibili = await api.async_get_supplies()
            except Exception:  # noqa: BLE001 - see comment in async_step_user
                # The code was already exchanged: it is single-use, so a retry
                # of this step would not fix anything without logging in again.
                _LOGGER.exception("Errore imprevisto nel recupero dei POD")
                return self.async_abort(reason="supplies_failed")

        if not self._pods_disponibili:
            return self.async_abort(reason="no_pods_found")

        def crea_entry(pods: list[str]):
            titolo = pods[0] if len(pods) == 1 else f"{len(pods)} POD"
            return self.async_create_entry(
                title=titolo,
                data={CONF_PODS: pods, CONF_REFRESH_TOKEN: self._refresh_token},
            )

        # With a single POD on the account there is nothing to choose.
        if len(self._pods_disponibili) == 1:
            return crea_entry([self._pods_disponibili[0]["IdPod"]])

        if user_input is not None:
            scelti = user_input[CONF_PODS]
            if not scelti:
                return self.async_show_form(
                    step_id="pod",
                    data_schema=self._schema_multi_pod(),
                    errors={"pods": "nessun_pod_selezionato"},
                )
            return crea_entry(scelti)

        return self.async_show_form(step_id="pod", data_schema=self._schema_multi_pod())

    def _schema_multi_pod(self) -> vol.Schema:
        pod_ids = [p["IdPod"] for p in self._pods_disponibili]
        opzioni = [{"value": p["IdPod"], "label": _etichetta_pod(p)} for p in self._pods_disponibili]
        return vol.Schema({
            vol.Required(CONF_PODS, default=pod_ids): selector.SelectSelector(
                selector.SelectSelectorConfig(options=opzioni, multiple=True)
            )
        })

    # ------------------------------------------------------------------
    # Reauth: same browser login, no POD selection
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EdistribuzioneOptionsFlow:
        return EdistribuzioneOptionsFlow()


class EdistribuzioneOptionsFlow(config_entries.OptionsFlow):
    """POD role, add/remove POD, request time - after the initial setup."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["tipo_pod", "aggiungi_pod", "rimuovi_pod", "orario"],
        )

    async def async_step_tipo_pod(self, user_input: dict[str, Any] | None = None):
        pods = list(self.config_entry.data.get(CONF_PODS, []))
        tipi_attuali = dict(self.config_entry.options.get(CONF_TIPO_POD, {}))

        if user_input is not None:
            nuovi_tipi = {pod: user_input[f"tipo_{pod}"] for pod in pods}
            nuove_opzioni = {**self.config_entry.options, CONF_TIPO_POD: nuovi_tipi}
            # Updated BEFORE the reload, so the sensors are rebuilt with the new
            # role. async_create_entry(data=...) below REPLACES entry.options
            # entirely with 'data' when the options flow ends: a data={} would
            # also wipe a previously set CONF_ORA_RICHIESTA, so the same complete
            # dict already applied is passed again (no different second write,
            # just the final confirmation the flow requires).
            self.hass.config_entries.async_update_entry(self.config_entry, options=nuove_opzioni)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data=nuove_opzioni)

        # Labels in translations/*.json, under selector.tipo_pod.
        opzioni_ruolo = [TIPO_POD_SCAMBIO, TIPO_POD_PRODUZIONE]
        schema = {
            vol.Required(
                f"tipo_{pod}", default=tipi_attuali.get(pod, TIPO_POD_DEFAULT)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=opzioni_ruolo, translation_key="tipo_pod")
            )
            for pod in pods
        }
        return self.async_show_form(step_id="tipo_pod", data_schema=vol.Schema(schema))

    async def async_step_orario(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            ora = int(user_input[CONF_ORA_RICHIESTA])
            return self.async_create_entry(
                title="", data={**self.config_entry.options, CONF_ORA_RICHIESTA: ora}
            )

        attuale = self.config_entry.options.get(CONF_ORA_RICHIESTA, ORA_MINIMA_RICHIESTA)
        return self.async_show_form(
            step_id="orario",
            data_schema=vol.Schema({
                vol.Required(CONF_ORA_RICHIESTA, default=attuale): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=23, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                )
            }),
            description_placeholders={"ora_attuale": str(attuale)},
        )

    async def async_step_aggiungi_pod(self, user_input: dict[str, Any] | None = None):
        pods_attuali = list(self.config_entry.data.get(CONF_PODS, []))

        if user_input is not None:
            nuovi = user_input.get("pods_da_aggiungere", [])
            if not nuovi:
                return self.async_abort(reason="nessun_pod_selezionato")
            pods_finali = pods_attuali + [p for p in nuovi if p not in pods_attuali]
            new_data = {**self.config_entry.data, CONF_PODS: pods_finali}
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            # data=... here is the OPTIONS dict (not data) the options flow ends
            # with: pass the current options unchanged, not {}, otherwise it
            # would wipe the already set CONF_TIPO_POD/CONF_ORA_RICHIESTA.
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        session = async_get_clientsession(self.hass)
        auth = AuthClient(session)
        try:
            tokens = await auth.async_refresh_access_token(
                self.config_entry.data[CONF_REFRESH_TOKEN]
            )
        except AccessoBloccato:
            _LOGGER.warning("Opzioni: E-Distribuzione ha risposto con la verifica antibot")
            return self.async_abort(reason="accesso_bloccato")
        except AppBloccata as err:
            _LOGGER.warning("Opzioni: E-Distribuzione blocca il client OAuth (%s)", err)
            return self.async_abort(reason="app_bloccata")
        except TokenNonValido:
            # Same reaction as the coordinator: ask for a new login, which
            # keeps PODs, options and history.
            self.config_entry.async_start_reauth(self.hass)
            return self.async_abort(reason="accesso_scaduto")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Refresh del token fallito nelle opzioni")
            return self.async_abort(
                reason="refresh_failed",
                description_placeholders={"errore": _descrivi_eccezione(err)},
            )

        api = ApiClient(session, tokens.access_token)
        try:
            tutti_pod = await api.async_get_supplies()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Recupero POD fallito nelle opzioni")
            return self.async_abort(reason="supplies_failed")

        pods_disponibili = [p for p in tutti_pod if p["IdPod"] not in pods_attuali]
        if not pods_disponibili:
            return self.async_abort(reason="nessun_pod_da_aggiungere")

        opzioni = [{"value": p["IdPod"], "label": _etichetta_pod(p)} for p in pods_disponibili]

        return self.async_show_form(
            step_id="aggiungi_pod",
            data_schema=vol.Schema({
                vol.Required("pods_da_aggiungere", default=[]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=opzioni, multiple=True)
                )
            }),
            description_placeholders={
                "pod_correnti": ", ".join(pods_attuali) or testo(self.hass.config.language, "nessun_pod")
            },
        )

    async def async_step_rimuovi_pod(self, user_input: dict[str, Any] | None = None):
        pods = list(self.config_entry.data.get(CONF_PODS, []))
        if not pods:
            return self.async_abort(reason="nessun_pod")

        if user_input is not None:
            da_rimuovere = set(user_input.get("pods_da_rimuovere", []))
            if len(da_rimuovere) >= len(pods):
                return self.async_show_form(
                    step_id="rimuovi_pod",
                    data_schema=self._schema_rimuovi(pods),
                    errors={"pods_da_rimuovere": "non_puoi_rimuoverli_tutti"},
                )
            pods_rimasti = [p for p in pods if p not in da_rimuovere]
            new_data = {**self.config_entry.data, CONF_PODS: pods_rimasti}
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            # See the comment in async_step_aggiungi_pod: 'data' here is the
            # options the flow ends with, not {}.
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        return self.async_show_form(step_id="rimuovi_pod", data_schema=self._schema_rimuovi(pods))

    @staticmethod
    def _schema_rimuovi(pods: list[str]) -> vol.Schema:
        return vol.Schema({
            vol.Required("pods_da_rimuovere", default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=pods, multiple=True)
            )
        })
