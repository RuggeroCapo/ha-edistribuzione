"""E-Distribuzione sensors.

The real data goes into the external statistics (statistics.py). The
diagnostic sensors only give an at-a-glance view of the import status - not
one sensor per band/magnitude, just the minimum to tell whether the
integration is working. The current ARERA time band is computed locally from
the calendar (fasce.py), with no request to E-Distribuzione.
"""
from __future__ import annotations

from datetime import date, datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import fasce
from .const import DOMAIN, MAGNITUDE_IMMESSA, MAGNITUDE_PRELEVATA, TIPO_POD_PRODUZIONE
from .coordinator import EdistribuzioneCoordinator
from .device_helpers import assicura_dispositivo_padre, collega_al_padre
from .testi import testo


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Sensor platform entry point, called by Home Assistant."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(build_entities(hass, coordinator))


def _device_info_account(entry: ConfigEntry, lingua: str | None) -> DeviceInfo:
    """"Parent" device for all the PODs of this config entry.

    The "model" has no translation key in HA: it follows the server language
    (see testi.py)."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="E-Distribuzione",
        manufacturer="E-Distribuzione",
        model=testo(lingua, "modello_account"),
    )


def _device_info_pod(
    entry: ConfigEntry, pod: str, ruolo: str, lingua: str | None, id_padre: str | None = None
) -> DeviceInfo:
    """Device for a single POD, linked to the account.

    The "model" depends on the role chosen by the user (see
    EdistribuzioneCoordinator.tipo_pod) - purely cosmetic, it does not affect
    which data is requested."""
    chiave = "modello_produzione" if ruolo == TIPO_POD_PRODUZIONE else "modello_prelievo"
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{pod}")},
        name=f"POD {pod}",
        manufacturer="E-Distribuzione",
        model=testo(lingua, chiave),
    )
    return collega_al_padre(info, {(DOMAIN, entry.entry_id)}, id_padre)


def build_entities(hass, coordinator: EdistribuzioneCoordinator) -> list[SensorEntity]:
    """Build the sensor entities for a config entry: one device per configured
    POD plus a shared "account" one."""
    entry = coordinator.entry

    # The "account" device must be registered BEFORE the per-POD ones, which
    # reference it as parent: on HA 2026.8+ its internal id is needed, and that
    # exists only after registration.
    id_padre = assicura_dispositivo_padre(
        hass, entry.entry_id, dict(_device_info_account(entry, coordinator.lingua))
    )

    entities: list[SensorEntity] = [
        PodConfiguratiSensor(coordinator, entry),
        FasciaCorrenteSensor(coordinator, entry),
    ]
    for pod in coordinator.pods:
        entities.append(UltimaDataDisponibileSensor(coordinator, entry, pod, id_padre))
        entities.append(ConsumoGiornoSensor(coordinator, entry, pod, id_padre, immessa=False))
        entities.append(ConsumoGiornoSensor(coordinator, entry, pod, id_padre, immessa=True))
        entities.append(PiccoPotenzaSensor(coordinator, entry, pod, id_padre, mese=False))
        entities.append(PiccoPotenzaSensor(coordinator, entry, pod, id_padre, mese=True))
        entities.append(PotenzaContrattoSensor(coordinator, entry, pod, id_padre))
        if coordinator.ha_costi(pod):
            entities.append(CostoSensor(coordinator, entry, pod, id_padre, mese=False))
            entities.append(CostoSensor(coordinator, entry, pod, id_padre, mese=True))
    return entities


class PodConfiguratiSensor(SensorEntity):
    """Shows how many PODs are configured in this instance - lives on the
    "account" device, whose real existence is also what makes via_device of the
    per-POD devices work."""

    _attr_has_entity_name = True
    _attr_translation_key = "pod_configurati"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: EdistribuzioneCoordinator, entry: ConfigEntry) -> None:
        super().__init__()
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_pod_configurati"
        self._attr_native_value = len(coordinator.pods)
        self._attr_device_info = _device_info_account(entry, coordinator.lingua)

    @property
    def extra_state_attributes(self):
        return {"pods": list(self.coordinator.pods)}


class FasciaCorrenteSensor(SensorEntity):
    """ARERA time band in force now (F1/F2/F3), on the "account" device.

    Computed from the calendar in fasce.py (Italian time, national holidays),
    so it keeps working while E-Distribuzione cannot be reached. It changes
    state exactly at the band boundaries, with no polling: each update
    schedules the next one at fasce.prossimo_cambio().
    """

    _attr_has_entity_name = True
    _attr_translation_key = "fascia_corrente"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(fasce.FASCE)
    _attr_icon = "mdi:clock-time-four-outline"
    _attr_should_poll = False

    def __init__(self, coordinator: EdistribuzioneCoordinator, entry: ConfigEntry) -> None:
        super().__init__()
        self._attr_unique_id = f"{entry.entry_id}_fascia_corrente"
        self._attr_device_info = _device_info_account(entry, coordinator.lingua)
        self._annulla_timer: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        self._aggiorna(dt_util.utcnow())
        self.async_on_remove(self._annulla)

    @callback
    def _annulla(self) -> None:
        if self._annulla_timer is not None:
            self._annulla_timer()
            self._annulla_timer = None

    @callback
    def _aggiorna(self, adesso: datetime) -> None:
        self._attr_native_value = fasce.fascia(adesso)
        cambio = fasce.prossimo_cambio(adesso)
        self._attr_extra_state_attributes = {
            "prossima_fascia": fasce.fascia(cambio),
            "prossimo_cambio": cambio,
        }
        self._annulla_timer = async_track_point_in_utc_time(self.hass, self._aggiorna, cambio)
        self.async_write_ha_state()


class _SensoreRiepilogo(CoordinatorEntity[EdistribuzioneCoordinator], SensorEntity):
    """Diagnostic sensor of one POD, read from coordinator.riepilogo.

    Always available: the values come from data already imported into Home
    Assistant, which stays valid while E-Distribuzione cannot be reached
    (an hourly update that fails would otherwise make it unavailable).
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._pod = pod
        self._attr_device_info = _device_info_pod(
            entry, pod, coordinator.tipo_pod(pod), coordinator.lingua, id_padre
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def _riepilogo(self) -> dict:
        return self.coordinator.riepilogo.get(self._pod) or {}


class UltimaDataDisponibileSensor(_SensoreRiepilogo):
    """Shows the last date for which data actually arrived (in at least one of
    the two directions) for a POD - reads the real state of the external
    statistics, not just whether the last cycle ran successfully."""

    _attr_translation_key = "ultima_data_disponibile"
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:calendar-check"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._attr_unique_id = f"{entry.entry_id}_{pod}_ultima_data_disponibile"

    @property
    def native_value(self) -> date | None:
        valore = self._riepilogo.get("ultima_data_disponibile")
        return date.fromisoformat(valore) if valore else None


class ConsumoGiornoSensor(_SensoreRiepilogo):
    """Energy (kWh) of the last imported day for ONE direction of a POD, with
    that day in the 'giorno' attribute. Deliberately without the 'energy'
    state_class: that value lives in the external statistics (statistics.py),
    not here - this sensor is diagnostic only."""

    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:lightning-bolt"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
        *,
        immessa: bool = False,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._direzione = MAGNITUDE_IMMESSA if immessa else MAGNITUDE_PRELEVATA
        chiave_unique = "immessa" if immessa else "prelevata"
        self._attr_unique_id = f"{entry.entry_id}_{pod}_consumo_giorno_{chiave_unique}"
        self._attr_translation_key = self._chiave_traduzione(coordinator, pod, immessa)

    @staticmethod
    def _chiave_traduzione(coordinator: EdistribuzioneCoordinator, pod: str, immessa: bool) -> str:
        """Name key in translations/*.json, depending on the role chosen by the
        user for this POD - same label as the matching statistic (see
        EdistribuzioneCoordinator._nome_serie)."""
        if coordinator.tipo_pod(pod) == TIPO_POD_PRODUZIONE:
            return "produzione_ultimo_giorno" if immessa else "prelievo_tecnico_ultimo_giorno"
        return "immissione_ultimo_giorno" if immessa else "prelievo_ultimo_giorno"

    @property
    def _ultimo_giorno(self) -> dict:
        return (self._riepilogo.get("ultimo_giorno") or {}).get(self._direzione) or {}

    @property
    def native_value(self) -> float | None:
        return self._ultimo_giorno.get("kwh")

    @property
    def extra_state_attributes(self):
        return {"giorno": self._ultimo_giorno.get("giorno")}


class CostoSensor(_SensoreRiepilogo):
    """Cost (EUR, VAT included) of the last imported day, or of its month up
    to that day, computed with the tariff set in the options (see costi.py).
    Not diagnostic: it is the figure to put on a dashboard. Like the kWh
    sensors, no state_class: the history lives in the cost statistic."""

    _attr_entity_category = None
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:currency-eur"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
        *,
        mese: bool,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._mese = mese
        chiave = "spesa_mese" if mese else "costo_ultimo_giorno"
        self._attr_translation_key = chiave
        self._attr_unique_id = f"{entry.entry_id}_{pod}_{chiave}"

    @property
    def _costi(self) -> dict:
        return self._riepilogo.get("costi") or {}

    @property
    def native_value(self) -> float | None:
        return self._costi.get("costo_mese" if self._mese else "costo_giorno")

    @property
    def extra_state_attributes(self):
        if self._mese:
            return {"mese": self._costi.get("mese"), "fino_al": self._costi.get("giorno")}
        return {"giorno": self._costi.get("giorno")}


class PiccoPotenzaSensor(_SensoreRiepilogo):
    """Highest 15-minute average power drawn (kW) on the last imported day,
    or in its month up to that day (see potenza.py). Attributes: when, ARERA
    band, and share of the available power."""

    _attr_entity_category = None
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "kW"
    _attr_icon = "mdi:flash-alert"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
        *,
        mese: bool,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._chiave = "picco_mese" if mese else "picco_giorno"
        self._attr_translation_key = f"{self._chiave}_potenza"
        self._attr_unique_id = f"{entry.entry_id}_{pod}_{self._chiave}_potenza"

    @property
    def _potenza(self) -> dict:
        return self._riepilogo.get("potenza") or {}

    @property
    def _picco(self) -> dict:
        return self._potenza.get(self._chiave) or {}

    @property
    def native_value(self) -> float | None:
        return self._picco.get("kw")

    @property
    def extra_state_attributes(self):
        attributi = {
            "quando": self._picco.get("quando"),
            "fascia": self._picco.get("fascia"),
            "percentuale_disponibile": self._picco.get("percentuale_disponibile"),
        }
        if self._chiave == "picco_mese":
            attributi |= {"mese": self._potenza.get("mese"), "fino_al": self._potenza.get("giorno")}
        return attributi


class PotenzaContrattoSensor(_SensoreRiepilogo):
    """Contract power (kW) of the POD from getSupplies, with the available
    power (usually 10% more) in the attributes."""

    _attr_translation_key = "potenza_impegnata"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "kW"
    _attr_icon = "mdi:gauge"

    def __init__(
        self,
        coordinator: EdistribuzioneCoordinator,
        entry: ConfigEntry,
        pod: str,
        id_padre: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry, pod, id_padre)
        self._attr_unique_id = f"{entry.entry_id}_{pod}_potenza_impegnata"

    @property
    def _contratto(self) -> dict:
        return self.coordinator.contratto.get(self._pod) or {}

    @property
    def native_value(self) -> float | None:
        return self._contratto.get("impegnata")

    @property
    def extra_state_attributes(self):
        return {"potenza_disponibile": self._contratto.get("disponibile")}
