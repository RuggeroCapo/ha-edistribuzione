"""Automatic (idempotent) Energy Dashboard configuration.

Adds the configured PODs' statistics to the Energy Dashboard sources, based on
the role chosen for each one (see EdistribuzioneCoordinator.tipo_pod):

- exchange: a "grid" source with consumption as import and injection as export;
- production: a "solar" source with injection as production (the consumption of
  a production POD stays available but does not go into the Energy Dashboard -
  typically the inverter's standby, not real consumption).

It does not overwrite or duplicate: if a source with the same import (grid) or
production (solar) statistic_id already exists, it is left intact - including
any cost configuration the user added by hand in the UI. Safe to call multiple
times.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.energy.data import (
    EnergyPreferencesUpdate,
    SourceType,
    async_get_manager,
)
from homeassistant.core import HomeAssistant

from .const import TIPO_POD_PRODUZIONE
from .statistics import statistic_id_costo, statistic_ids

if TYPE_CHECKING:
    # Type only: the coordinator imports this module to link the costs.
    from .coordinator import EdistribuzioneCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_configura_energy_dashboard(
    hass: HomeAssistant, coordinators: list[EdistribuzioneCoordinator]
) -> list[str]:
    """Add the missing sources for every POD of the given coordinators.

    Returns the labels (POD + role) actually added; empty if there was nothing
    to add (already fully configured).
    """
    manager = await async_get_manager(hass)
    sorgenti: list[SourceType] = list(manager.data["energy_sources"]) if manager.data else []

    # A POD is "already configured" if its statistic_id appears as import (grid
    # or solar) or as export (grid) of an existing source, whoever created it
    # (this action or the user by hand): adding a second one for the same data
    # makes no sense.
    from_esistenti = {
        s.get("stat_energy_from") for s in sorgenti if s.get("type") in ("grid", "solar")
    }
    to_esistenti = {s.get("stat_energy_to") for s in sorgenti if s.get("type") == "grid"}

    aggiunte: list[str] = []
    for coordinator in coordinators:
        for pod in coordinator.pods:
            prelevata, immessa = statistic_ids(pod)
            ruolo = coordinator.tipo_pod(pod)

            if ruolo == TIPO_POD_PRODUZIONE:
                if immessa in from_esistenti:
                    continue
                sorgenti.append({
                    "type": "solar",
                    "stat_energy_from": immessa,
                    "name": f"POD {pod}",
                })
                from_esistenti.add(immessa)
                aggiunte.append(f"{pod} (produzione)")
            else:
                if prelevata in from_esistenti or immessa in to_esistenti:
                    continue
                sorgenti.append({
                    "type": "grid",
                    "stat_energy_from": prelevata,
                    "stat_energy_to": immessa,
                    "stat_cost": statistic_id_costo(pod) if coordinator.ha_costi(pod) else None,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "stat_compensation": None,
                    "entity_energy_price_export": None,
                    "number_energy_price_export": None,
                    "cost_adjustment_day": 0.0,
                    "name": f"POD {pod}",
                })
                from_esistenti.add(prelevata)
                to_esistenti.add(immessa)
                aggiunte.append(f"{pod} (scambio)")

    if not aggiunte:
        _LOGGER.info(
            "Energy Dashboard: nessuna sorgente nuova da aggiungere, già tutto configurato"
        )
        return aggiunte

    update: EnergyPreferencesUpdate = {"energy_sources": sorgenti}
    await manager.async_update(update)
    _LOGGER.info("Energy Dashboard: aggiunte le sorgenti per %s", ", ".join(aggiunte))
    return aggiunte


def _ha_un_costo(sorgente: dict) -> bool:
    return any(
        sorgente.get(campo)
        for campo in ("stat_cost", "entity_energy_price", "number_energy_price")
    )


async def async_collega_costo(hass: HomeAssistant, pod: str, *, attivo: bool) -> bool:
    """Link (attivo=True) or unlink the POD's cost statistic on its grid source.

    Never overrides a cost the user set by hand (another statistic, a price
    entity or a fixed price): it only fills an empty one, and only removes its
    own. Returns True if the preferences changed."""
    manager = await async_get_manager(hass)
    if not manager.data:
        return False
    prelevata, _ = statistic_ids(pod)
    costo = statistic_id_costo(pod)

    sorgenti: list[SourceType] = list(manager.data["energy_sources"])
    cambiato = False
    for i, sorgente in enumerate(sorgenti):
        if sorgente.get("type") != "grid" or sorgente.get("stat_energy_from") != prelevata:
            continue
        if attivo and not _ha_un_costo(sorgente):
            sorgenti[i] = {**sorgente, "stat_cost": costo}
            cambiato = True
        elif not attivo and sorgente.get("stat_cost") == costo:
            sorgenti[i] = {**sorgente, "stat_cost": None}
            cambiato = True

    if cambiato:
        await manager.async_update({"energy_sources": sorgenti})
        _LOGGER.info(
            "Energy Dashboard: costo del POD %s %s", pod, "collegato" if attivo else "scollegato"
        )
    return cambiato
