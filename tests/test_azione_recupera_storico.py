"""Data of the recupera_storico action: every error must say what to fix.

The action is often written by hand in YAML: dates without quotes, swapped or
in day/month/year format, the POD code or an entity in place of the device_id.
Up to v0.6.0 Home Assistant answered with messages such as "Invalid date
specified" or "Device not found", without saying how to fix them. Here the
integration is really loaded (devices included), with the network and the
fetch replaced.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from freezegun import freeze_time
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione.const import CONF_PODS, CONF_REFRESH_TOKEN, DOMAIN
from custom_components.edistribuzione.coordinator import EdistribuzioneCoordinator

from .conftest import POD_A, POD_B

OGGI = "2026-10-13 12:00:00"


@pytest.fixture
async def integrazione(hass):
    """Integration loaded with two PODs; returns (entry, recupero), where
    recupero replaces async_recupera_storico and records its arguments."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_REFRESH_TOKEN: "rt", CONF_PODS: [POD_A, POD_B]},
        title="2 POD",
    )
    entry.add_to_hass(hass)
    auth = Mock()
    auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(access_token="a", refresh_token="rt")
    )
    recupero = AsyncMock()
    with (
        patch("custom_components.edistribuzione.coordinator.AuthClient", return_value=auth),
        patch.object(
            EdistribuzioneCoordinator, "_async_update_data", AsyncMock(return_value={})
        ),
        patch.object(EdistribuzioneCoordinator, "async_recupera_storico", recupero),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, recupero


def _device_id(hass, identificativo: str) -> str:
    entry_id = identificativo.split("_", 1)[0]
    return next(
        d.id
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
        if (DOMAIN, identificativo) in d.identifiers
    )


async def _chiama(hass, dati: dict):
    with freeze_time(OGGI):
        await hass.services.async_call(DOMAIN, "recupera_storico", dati, blocking=True)


async def _errore(hass, dati: dict) -> ServiceValidationError:
    with pytest.raises(ServiceValidationError) as errore:
        await _chiama(hass, dati)
    assert errore.value.translation_domain == DOMAIN
    # Rendered the way the UI shows it: an unknown key would come back as the
    # bare key, a missing placeholder as a literal "{name}".
    messaggio = str(errore.value)
    assert messaggio != errore.value.translation_key
    assert "{" not in messaggio, messaggio
    return errore.value


# --- Valid calls -----------------------------------------------------------------


async def test_dispositivo_account_recupera_tutti_i_pod(hass, integrazione):
    entry, recupero = integrazione
    await _chiama(
        hass,
        {"device_id": _device_id(hass, entry.entry_id), "data_da": "2026-08-01", "data_a": "2026-08-31"},
    )
    recupero.assert_awaited_once_with(date(2026, 8, 1), date(2026, 8, 31), pod=None)


async def test_dispositivo_di_un_pod_recupera_solo_quello(hass, integrazione):
    entry, recupero = integrazione
    await _chiama(
        hass,
        {
            "device_id": _device_id(hass, f"{entry.entry_id}_{POD_B}"),
            "data_da": "2026-08-01",
            "data_a": "2026-08-31",
        },
    )
    assert recupero.await_args.kwargs == {"pod": POD_B}


@pytest.mark.parametrize(
    "valore",
    [
        # Date without quotes in the YAML editor of Developer tools: the
        # frontend reads it as a YAML date and sends it like this.
        "2026-08-01T00:00:00.000Z",
        "2026-8-1",
        " 2026-08-01 ",
        date(2026, 8, 1),
    ],
)
async def test_date_accettate(hass, integrazione, valore):
    entry, recupero = integrazione
    await _chiama(
        hass,
        {"device_id": _device_id(hass, entry.entry_id), "data_da": valore, "data_a": "2026-08-31"},
    )
    assert recupero.await_args.args[0] == date(2026, 8, 1)


async def test_device_id_come_destinazione_dell_azione(hass, integrazione):
    """With "target: device_id:" a list arrives."""
    entry, recupero = integrazione
    await _chiama(
        hass,
        {"device_id": [_device_id(hass, entry.entry_id)], "data_da": "2026-08-01", "data_a": "2026-08-02"},
    )
    recupero.assert_awaited_once()


# --- Dates ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("valore", "esempio"),
    [
        # Day/month/year: not accepted (01/08 is ambiguous), but the example
        # offers the same date in the right format.
        ("01/08/2026", "2026-08-01"),
        ("1.8.2026", "2026-08-01"),
        # Impossible date, any text: the example is yesterday.
        ("2026-02-30", "2026-10-12"),
        ("ieri", "2026-10-12"),
    ],
)
async def test_data_non_valida_con_esempio(hass, integrazione, valore, esempio):
    entry, recupero = integrazione
    errore = await _errore(
        hass, {"device_id": _device_id(hass, entry.entry_id), "data_da": valore, "data_a": "2026-08-31"}
    )
    assert errore.translation_key == "data_da_non_valida"
    assert errore.translation_placeholders == {"valore": valore, "esempio": esempio}
    recupero.assert_not_awaited()


async def test_data_di_fine_mancante(hass, integrazione):
    entry, _ = integrazione
    errore = await _errore(
        hass, {"device_id": _device_id(hass, entry.entry_id), "data_da": "2026-08-01"}
    )
    assert errore.translation_key == "data_a_mancante"


async def test_campo_sconosciuto(hass, integrazione):
    entry, _ = integrazione
    errore = await _errore(
        hass,
        {
            "device_id": _device_id(hass, entry.entry_id),
            "data_inizio": "2026-08-01",
            "data_a": "2026-08-31",
        },
    )
    assert errore.translation_key == "campo_sconosciuto"
    assert errore.translation_placeholders == {"campi": "data_inizio"}


# --- device_id ------------------------------------------------------------------


async def test_device_id_mancante_elenca_i_dispositivi(hass, integrazione):
    entry, _ = integrazione
    errore = await _errore(hass, {"data_da": "2026-08-01", "data_a": "2026-08-31"})
    assert errore.translation_key == "dispositivo_mancante"
    disponibili = errore.translation_placeholders["disponibili"]
    assert f"E-Distribuzione: {_device_id(hass, entry.entry_id)}" in disponibili
    assert f"POD {POD_A}: {_device_id(hass, f'{entry.entry_id}_{POD_A}')}" in disponibili


async def test_device_id_inesistente(hass, integrazione):
    entry, _ = integrazione
    errore = await _errore(
        hass, {"device_id": "abc123", "data_da": "2026-08-01", "data_a": "2026-08-31"}
    )
    assert errore.translation_key == "dispositivo_non_trovato"
    assert errore.translation_placeholders["device_id"] == "abc123"
    assert _device_id(hass, entry.entry_id) in errore.translation_placeholders["disponibili"]


async def test_codice_pod_al_posto_del_device_id_indica_quello_giusto(hass, integrazione):
    entry, _ = integrazione
    errore = await _errore(
        hass, {"device_id": POD_A.lower(), "data_da": "2026-08-01", "data_a": "2026-08-31"}
    )
    assert errore.translation_key == "dispositivo_e_un_pod"
    assert errore.translation_placeholders == {
        "valore": POD_A.lower(),
        "device_id": _device_id(hass, f"{entry.entry_id}_{POD_A}"),
    }


async def test_codice_di_un_pod_non_configurato(hass, integrazione):
    errore = await _errore(
        hass, {"device_id": "IT001E99999999", "data_da": "2026-08-01", "data_a": "2026-08-31"}
    )
    assert errore.translation_key == "pod_non_configurato"
    assert errore.translation_placeholders == {"pod": "IT001E99999999", "pods": f"{POD_A}, {POD_B}"}


async def test_entita_al_posto_del_device_id(hass, integrazione):
    errore = await _errore(
        hass,
        {"device_id": "sensor.pod_ultima_data", "data_da": "2026-08-01", "data_a": "2026-08-31"},
    )
    assert errore.translation_key == "dispositivo_e_un_entita"


async def test_dispositivo_di_un_altra_integrazione(hass, integrazione):
    altra = MockConfigEntry(domain="hue")
    altra.add_to_hass(hass)
    lampada = dr.async_get(hass).async_get_or_create(
        config_entry_id=altra.entry_id, identifiers={("hue", "lampada")}, name="Lampada"
    )
    errore = await _errore(
        hass, {"device_id": lampada.id, "data_da": "2026-08-01", "data_a": "2026-08-31"}
    )
    assert errore.translation_key == "dispositivo_altra_integrazione"
    assert errore.translation_placeholders["nome"] == "Lampada"


async def test_dispositivo_di_una_configurazione_non_attiva(hass, integrazione):
    """The device exists, but its setup did not start (for example because
    access expired): that must be said, not "not found"."""
    ferma = MockConfigEntry(domain=DOMAIN, data={CONF_REFRESH_TOKEN: "rt", CONF_PODS: ["IT001E1"]})
    ferma.add_to_hass(hass)
    dispositivo = dr.async_get(hass).async_get_or_create(
        config_entry_id=ferma.entry_id, identifiers={(DOMAIN, ferma.entry_id)}, name="Vecchio"
    )
    errore = await _errore(
        hass, {"device_id": dispositivo.id, "data_da": "2026-08-01", "data_a": "2026-08-31"}
    )
    assert errore.translation_key == "dispositivo_senza_configurazione"
    assert errore.translation_placeholders == {"nome": "Vecchio"}


async def test_piu_dispositivi_insieme(hass, integrazione):
    entry, _ = integrazione
    errore = await _errore(
        hass,
        {
            "device_id": [
                _device_id(hass, f"{entry.entry_id}_{POD_A}"),
                _device_id(hass, f"{entry.entry_id}_{POD_B}"),
            ],
            "data_da": "2026-08-01",
            "data_a": "2026-08-31",
        },
    )
    assert errore.translation_key == "un_solo_dispositivo"


# --- Current time band, with the integration loaded -----------------------------


async def test_sensore_fascia_corrente_creato_sull_account(hass, integrazione):
    entry, _ = integrazione
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_fascia_corrente"
    )
    stato = hass.states.get(entity_id)
    assert stato.state in ("f1", "f2", "f3")
    assert stato.attributes["prossima_fascia"] in ("f1", "f2", "f3")
    assert stato.attributes["prossima_fascia"] != stato.state
