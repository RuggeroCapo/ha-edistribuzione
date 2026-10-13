"""_prossima_richiesta: which range to request for a POD in this cycle.

Covers two distinct behaviours:

- Regression found in live tests with two PODs on the same entry: only the
  first POD in the list did the immediate check fetch on the first start,
  because that decision was driven by a flag SHARED by the whole entry
  (CONF_DATA_INSTALLAZIONE) instead of by a per-POD condition ("does this POD
  already have data?"). With two PODs both without data, the second one got
  nothing until the configured hour (19:00 by default), because by the time
  the cycle reached it the flag had already been set by the first.

- The periodic recheck of the last GIORNI_RICONTROLLO days (not only the most
  recent one): E-Distribuzione can correct a day already published. The
  "at most one request a day per POD" throttle stays the same as always
  (comparing ultima_disponibile and 'atteso', which does not change until the
  day changes) - only the WIDTH of the requested window changes when it
  fires.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.edistribuzione.const import CONF_DATA_INSTALLAZIONE, GIORNI_RICONTROLLO

from .conftest import POD_A, POD_B

OGGI = "2026-09-23 15:00:00"  # before 19:00 (default ORA_MINIMA_RICHIESTA)
OGGI_DOPO_ORARIO = "2026-09-23 20:00:00"  # after 19:00
ATTESO = date(2026, 9, 22)  # today - RITARDO_DATI_GIORNI
INIZIO_RICONTROLLO = ATTESO - timedelta(days=GIORNI_RICONTROLLO - 1)  # 2026-09-20


@pytest.fixture(autouse=True)
async def _fuso_utc(hass):
    """The request hour (_ora_richiesta) is compared with adesso.hour in the
    configured time zone: without pinning it to UTC, the tests' default time
    zone would trigger or skip the "after 19:00" check unpredictably,
    depending on the machine/environment."""
    await hass.config.async_set_time_zone("UTC")


def _con_ultima_data(per_pod: dict[str, date | None]):
    async def _fake(hass, pod):
        return per_pod.get(pod)

    return patch(
        "custom_components.edistribuzione.coordinator.async_get_ultima_data_disponibile",
        AsyncMock(side_effect=_fake),
    )


async def test_ogni_pod_senza_dati_fa_il_fetch_immediato_anche_prima_dell_ora(
    hass, make_edist_coordinator
):
    """With two PODs both without data (first start), BOTH must request the
    recheck window right away - not only the first in the list, and not only
    the most recent day."""
    coordinator = make_edist_coordinator(pods=[POD_A, POD_B])

    with freeze_time(OGGI), _con_ultima_data({POD_A: None, POD_B: None}):
        richiesta_a = await coordinator._prossima_richiesta(POD_A)
        richiesta_b = await coordinator._prossima_richiesta(POD_B)

    assert richiesta_a == (INIZIO_RICONTROLLO, ATTESO)
    assert richiesta_b == (INIZIO_RICONTROLLO, ATTESO)


async def test_pod_con_dati_aspetta_l_orario_configurato(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(pods=[POD_A])

    with freeze_time(OGGI), _con_ultima_data({POD_A: date(2026, 9, 21)}):
        richiesta = await coordinator._prossima_richiesta(POD_A)

    assert richiesta is None


async def test_pod_aggiunto_dopo_su_entry_gia_avviata_fa_comunque_il_fetch_immediato(
    hass, make_edist_coordinator
):
    """A POD added from the options after the entry already exists (so with
    CONF_DATA_INSTALLAZIONE set long ago) must still start right away,
    because there is no data for IT yet - exactly the case the bug broke."""
    coordinator = make_edist_coordinator(
        pods=[POD_A, POD_B], data={CONF_DATA_INSTALLAZIONE: "2026-09-01"}
    )

    with freeze_time(OGGI), _con_ultima_data({POD_A: date(2026, 9, 21), POD_B: None}):
        richiesta_a = await coordinator._prossima_richiesta(POD_A)
        richiesta_b = await coordinator._prossima_richiesta(POD_B)

    assert richiesta_a is None
    assert richiesta_b == (INIZIO_RICONTROLLO, ATTESO)


async def test_dopo_l_orario_richiede_la_finestra_di_ricontrollo_non_solo_atteso(
    hass, make_edist_coordinator
):
    """Past the configured hour, a POD not yet updated today requests
    GIORNI_RICONTROLLO days up to 'atteso', in a single request - not only
    the last day."""
    coordinator = make_edist_coordinator(pods=[POD_A])

    with freeze_time(OGGI_DOPO_ORARIO), _con_ultima_data({POD_A: date(2026, 9, 21)}):
        richiesta = await coordinator._prossima_richiesta(POD_A)

    assert richiesta == (INIZIO_RICONTROLLO, ATTESO)


async def test_gia_ricontrollato_oggi_non_richiede_di_nuovo(hass, make_edist_coordinator):
    """At most one request a day per POD: if 'ultima_disponibile' has
    already reached 'atteso' (today's recheck already happened), the
    following hourly cycles do not request again until the next day."""
    coordinator = make_edist_coordinator(pods=[POD_A])

    with freeze_time(OGGI_DOPO_ORARIO), _con_ultima_data({POD_A: ATTESO}):
        richiesta = await coordinator._prossima_richiesta(POD_A)

    assert richiesta is None


async def test_giorni_arretrati_piu_vecchi_della_finestra_allargano_l_inizio(
    hass, make_edist_coordinator
):
    """A backlog day older than the recheck window (e.g. stuck in the queue
    after a previous error) widens the range backwards to include it, still
    in a single request."""
    from custom_components.edistribuzione.const import CONF_GIORNI_DA_RIPROVARE

    vecchio = (INIZIO_RICONTROLLO - timedelta(days=5)).isoformat()
    coordinator = make_edist_coordinator(
        pods=[POD_A],
        data={CONF_GIORNI_DA_RIPROVARE: {POD_A: {vecchio: "2026-09-15"}}},
    )

    with freeze_time(OGGI_DOPO_ORARIO), _con_ultima_data({POD_A: date(2026, 9, 21)}):
        richiesta = await coordinator._prossima_richiesta(POD_A)

    assert richiesta == (date.fromisoformat(vecchio), ATTESO)


async def test_giorni_arretrati_dentro_la_finestra_non_la_allargano(
    hass, make_edist_coordinator
):
    """A backlog day already inside the recheck window (e.g. yesterday, left
    in the queue by a failed cycle) is covered by the periodic recheck itself
    - it does not need to be treated as backlog."""
    from custom_components.edistribuzione.const import CONF_GIORNI_DA_RIPROVARE

    dentro_la_finestra = (INIZIO_RICONTROLLO + timedelta(days=1)).isoformat()
    coordinator = make_edist_coordinator(
        pods=[POD_A],
        data={CONF_GIORNI_DA_RIPROVARE: {POD_A: {dentro_la_finestra: "2026-09-21"}}},
    )

    with freeze_time(OGGI_DOPO_ORARIO), _con_ultima_data({POD_A: date(2026, 9, 21)}):
        richiesta = await coordinator._prossima_richiesta(POD_A)

    assert richiesta == (INIZIO_RICONTROLLO, ATTESO)
