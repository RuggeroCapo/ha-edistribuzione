"""Config flow: browser login (pasted redirect URL), POD selection, options
flow (POD role, add/remove POD, request time) and reauth.

Everything that touches the network is replaced: the AuthClient / ApiClient
clients and the aiohttp sessions. No ARERA/municipality wizard (a single
distributor, a single flow): it starts straight from the "user" step.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione import api as edist_api
from custom_components.edistribuzione import auth as edist_auth
from custom_components.edistribuzione import config_flow as cf
from custom_components.edistribuzione.auth import (
    AccessoBloccato,
    AppBloccata,
    AutorizzazioneNegata,
    ParsingError,
    StateNonCorrispondente,
    TokenNonValido,
    UrlRedirectNonValido,
)
from custom_components.edistribuzione.const import (
    CONF_ORA_RICHIESTA,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    DOMAIN,
    TIPO_POD_PRODUZIONE,
    TIPO_POD_SCAMBIO,
)


@pytest.fixture(autouse=True)
def _mock_setup_entry():
    """Keeps a CREATE_ENTRY (or an async_reload after reauth/options) from
    starting the entry's real setup - coordinator, aiohttp session, first
    refresh: only the outcome of the flow matters here."""
    with patch("custom_components.edistribuzione.async_setup_entry", return_value=True):
        yield


@pytest.fixture
def edist_mocks(monkeypatch):
    """Replaces AuthClient / ApiClient and the aiohttp sessions. Default:
    the pasted URL is exchanged, a single POD on the account."""
    auth = Mock()
    auth.crea_link_autorizzazione = Mock(return_value="https://link-di-accesso")
    auth.async_scambia_url_redirect = AsyncMock(
        return_value=SimpleNamespace(access_token="acc", refresh_token="ref-nuovo")
    )
    auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(access_token="acc2", refresh_token="ref-nuovo")
    )
    api = Mock()
    api.async_get_supplies = AsyncMock(return_value=[{"IdPod": "IT001E00000009"}])

    monkeypatch.setattr(edist_auth, "AuthClient", Mock(return_value=auth))
    monkeypatch.setattr(edist_api, "ApiClient", Mock(return_value=api))
    monkeypatch.setattr(cf, "AuthClient", Mock(return_value=auth))
    monkeypatch.setattr(cf, "ApiClient", Mock(return_value=api))
    monkeypatch.setattr(cf, "async_get_clientsession", lambda *a, **k: Mock())
    return SimpleNamespace(auth=auth, api=api)


async def _fino_a_user(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})


# --- login in the browser -> pasted redirect URL ---------------------------

REDIRECT = "eneldist://redirect?code=abc&state=xyz"


async def _incolla(hass, res, testo=REDIRECT):
    return await hass.config_entries.flow.async_configure(
        res["flow_id"], {cf.CONF_URL_REDIRECT: testo}
    )


async def test_il_form_mostra_il_link_di_accesso(hass, edist_mocks):
    res = await _fino_a_user(hass)
    assert res["type"] == FlowResultType.FORM
    assert res["step_id"] == "user"
    assert res["description_placeholders"]["link"] == "https://link-di-accesso"


async def test_il_link_resta_lo_stesso_dopo_un_errore(hass, edist_mocks):
    """One AuthClient per flow: a wrong paste does not invalidate the link
    the user already opened."""
    edist_mocks.auth.async_scambia_url_redirect.side_effect = UrlRedirectNonValido("no")
    res = await _fino_a_user(hass)
    res = await _incolla(hass, res, "testo a caso")
    assert res["errors"] == {"base": "url_non_valido"}
    assert cf.AuthClient.call_count == 1


async def test_l_url_incollato_arriva_all_auth_client(hass, edist_mocks):
    res = await _fino_a_user(hass)
    await _incolla(hass, res)
    edist_mocks.auth.async_scambia_url_redirect.assert_awaited_once_with(REDIRECT)


@pytest.mark.parametrize(
    ("errore", "chiave"),
    [
        (UrlRedirectNonValido("no"), "url_non_valido"),
        (StateNonCorrispondente("no"), "state_diverso"),
        (AutorizzazioneNegata("access_denied", None), "autorizzazione_negata"),
        (TokenNonValido("invalid_grant"), "codice_scaduto"),
        (AccessoBloccato("antibot"), "accesso_bloccato"),
        (AppBloccata("app_blocked"), "app_bloccata"),
        (ParsingError("html"), "risposta_inattesa"),
        (aiohttp.ClientError("rete"), "cannot_connect"),
        (RuntimeError("boom"), "errore_imprevisto"),
    ],
)
async def test_ogni_errore_resta_sul_form(hass, edist_mocks, errore, chiave):
    edist_mocks.auth.async_scambia_url_redirect.side_effect = errore
    res = await _fino_a_user(hass)
    res = await _incolla(hass, res)
    assert res["type"] == FlowResultType.FORM
    assert res["step_id"] == "user"
    assert res["errors"] == {"base": chiave}


async def test_autorizzazione_negata_mostra_il_motivo(hass, edist_mocks):
    edist_mocks.auth.async_scambia_url_redirect.side_effect = AutorizzazioneNegata(
        "access_denied", "end-user denied"
    )
    res = await _fino_a_user(hass)
    res = await _incolla(hass, res)
    assert "end-user denied" in res["description_placeholders"]["messaggio"]


async def test_dopo_un_errore_si_puo_riprovare(hass, edist_mocks):
    edist_mocks.auth.async_scambia_url_redirect.side_effect = [
        TokenNonValido("scaduto"),
        SimpleNamespace(access_token="acc", refresh_token="ref-nuovo"),
    ]
    res = await _fino_a_user(hass)
    res = await _incolla(hass, res)
    res = await _incolla(hass, res)
    assert res["type"] == FlowResultType.CREATE_ENTRY


# --- POD selection ----------------------------------------------------------


async def test_un_solo_pod_crea_entry_subito(hass, edist_mocks):
    res = await _fino_a_user(hass)
    res = await _incolla(hass, res)
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_PODS] == ["IT001E00000009"]
    assert res["data"][CONF_REFRESH_TOKEN] == "ref-nuovo"


async def test_nessun_pod_sull_account_abortisce(hass, edist_mocks):
    edist_mocks.api.async_get_supplies.return_value = []
    res = await _fino_a_user(hass)
    res = await _incolla(hass, res)
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "no_pods_found"


async def test_recupero_pod_fallito_abortisce(hass, edist_mocks):
    edist_mocks.api.async_get_supplies.side_effect = RuntimeError("boom")
    res = await _fino_a_user(hass)
    res = await _incolla(hass, res)
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "supplies_failed"


async def test_piu_pod_si_scelgono(hass, edist_mocks):
    edist_mocks.api.async_get_supplies.return_value = [
        {"IdPod": "IT001E00000009"},
        {"IdPod": "IT001E00000010"},
    ]
    res = await _fino_a_user(hass)
    res = await _incolla(hass, res)
    assert res["step_id"] == "pod"

    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_PODS: ["IT001E00000010"]}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_PODS] == ["IT001E00000010"]


# --- reauth ------------------------------------------------------------------


def _entry_edist(hass, pods=("IT001E00000009",), options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PODS: list(pods), CONF_REFRESH_TOKEN: "ref-vecchio"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_aggiorna_refresh_token(hass, edist_mocks):
    entry = _entry_edist(hass)
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
    )
    assert res["step_id"] == "user"
    res = await _incolla(hass, res)
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "ref-nuovo"


# --- options flow ------------------------------------------------------------


async def test_opzioni_menu(hass):
    entry = _entry_edist(hass)
    res = await hass.config_entries.options.async_init(entry.entry_id)
    assert res["type"] == FlowResultType.MENU
    assert set(res["menu_options"]) == {"tipo_pod", "costi", "aggiungi_pod", "rimuovi_pod", "orario"}


async def test_opzioni_orario_salva_valore(hass):
    entry = _entry_edist(hass)
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "orario"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_ORA_RICHIESTA: 21}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ORA_RICHIESTA] == 21


async def test_opzioni_tipo_pod_default_e_scambio(hass):
    entry = _entry_edist(hass, pods=["IT001E00000009"])
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "tipo_pod"}
    )
    assert res["step_id"] == "tipo_pod"
    assert res["data_schema"]({})["tipo_IT001E00000009"] == TIPO_POD_SCAMBIO


async def test_opzioni_tipo_pod_salva_e_persiste(hass):
    entry = _entry_edist(hass, pods=["IT001E00000009"])
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "tipo_pod"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"tipo_IT001E00000009": TIPO_POD_PRODUZIONE}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TIPO_POD] == {"IT001E00000009": TIPO_POD_PRODUZIONE}


async def test_opzioni_tipo_pod_modificabile_di_nuovo(hass):
    """Changing the role is not a one-off: reopening the step must show the
    value already saved, not go back to the default."""
    entry = _entry_edist(
        hass, pods=["IT001E00000009"], options={CONF_TIPO_POD: {"IT001E00000009": TIPO_POD_PRODUZIONE}}
    )
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "tipo_pod"}
    )
    assert res["data_schema"]({})["tipo_IT001E00000009"] == TIPO_POD_PRODUZIONE


async def test_opzioni_aggiungi_pod(hass, edist_mocks):
    entry = _entry_edist(hass, pods=["IT001E00000009"])
    edist_mocks.api.async_get_supplies.return_value = [
        {"IdPod": "IT001E00000009"},
        {"IdPod": "IT001E00000010"},
    ]
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "aggiungi_pod"}
    )
    assert res["step_id"] == "aggiungi_pod"
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"pods_da_aggiungere": ["IT001E00000010"]}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_PODS] == ["IT001E00000009", "IT001E00000010"]


async def _opzioni_aggiungi_pod_con_rinnovo(hass, edist_mocks, errore):
    entry = _entry_edist(hass, pods=["IT001E00000009"])
    edist_mocks.auth.async_refresh_access_token.side_effect = errore
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "aggiungi_pod"}
    )
    return entry, res


async def test_opzioni_aggiungi_pod_con_accesso_scaduto_chiede_un_nuovo_login(
    hass, edist_mocks
):
    entry, res = await _opzioni_aggiungi_pod_con_rinnovo(
        hass, edist_mocks, TokenNonValido("invalid_grant")
    )
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "accesso_scaduto"
    flussi = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flussi] == ["reauth"]
    assert flussi[0]["context"]["entry_id"] == entry.entry_id


@pytest.mark.parametrize(
    ("errore", "motivo"),
    [
        (AppBloccata("app_blocked"), "app_bloccata"),
        (AccessoBloccato("antibot"), "accesso_bloccato"),
        (RuntimeError("boom"), "refresh_failed"),
    ],
)
async def test_opzioni_aggiungi_pod_rinnovo_fallito(hass, edist_mocks, errore, motivo):
    _, res = await _opzioni_aggiungi_pod_con_rinnovo(hass, edist_mocks, errore)
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == motivo
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_opzioni_rimuovi_pod_non_tutti(hass):
    entry = _entry_edist(hass, pods=["IT001E00000009", "IT001E00000010"])
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "rimuovi_pod"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {"pods_da_rimuovere": ["IT001E00000009", "IT001E00000010"]},
    )
    assert res["type"] == FlowResultType.FORM
    assert res["errors"] == {"pods_da_rimuovere": "non_puoi_rimuoverli_tutti"}


async def test_opzioni_rimuovi_pod(hass):
    entry = _entry_edist(hass, pods=["IT001E00000009", "IT001E00000010"])
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "rimuovi_pod"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"pods_da_rimuovere": ["IT001E00000010"]}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_PODS] == ["IT001E00000009"]


# --- costs ---------------------------------------------------------------------


async def _apri_costi(hass, entry):
    res = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "costi"}
    )


async def test_opzioni_costi_monoraria(hass):
    entry = _entry_edist(hass)
    res = await _apri_costi(hass, entry)
    assert res["step_id"] == "costi"
    res = await hass.config_entries.options.async_configure(res["flow_id"], {"tipo": "monoraria"})
    assert res["step_id"] == "costi_prezzi"
    campi = {str(k) for k in res["data_schema"].schema}
    assert "prezzo" in campi and "prezzo_f1" not in campi

    prezzi = {
        "prezzo": 0.12,
        "extra_kwh": 0.05,
        "quota_fissa_mese": 7.0,
        "potenza_kw": 3.0,
        "quota_potenza": 1.8,
        "iva": 10.0,
    }
    res = await hass.config_entries.options.async_configure(res["flow_id"], prezzi)
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[cf.CONF_TARIFFA] == {"tipo": "monoraria", **prezzi}


async def test_opzioni_costi_fasce_chiede_tre_prezzi(hass):
    entry = _entry_edist(hass)
    res = await _apri_costi(hass, entry)
    res = await hass.config_entries.options.async_configure(res["flow_id"], {"tipo": "fasce"})
    campi = {str(k) for k in res["data_schema"].schema}
    assert {"prezzo_f1", "prezzo_f2", "prezzo_f3"} <= campi and "prezzo" not in campi


async def test_opzioni_costi_nessuna_toglie_la_tariffa_e_tiene_il_resto(hass):
    entry = _entry_edist(
        hass,
        options={cf.CONF_TARIFFA: {"tipo": "monoraria", "prezzo": 0.1}, CONF_ORA_RICHIESTA: 20},
    )
    res = await _apri_costi(hass, entry)
    res = await hass.config_entries.options.async_configure(res["flow_id"], {"tipo": "nessuna"})
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert cf.CONF_TARIFFA not in entry.options
    assert entry.options[CONF_ORA_RICHIESTA] == 20
