"""Fixtures shared by the tests.

pytest_plugins/enable_custom_integrations is the standard pattern required by
pytest-homeassistant-custom-component for Home Assistant to actually load this
custom integration in the tests instead of ignoring it.
"""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione.const import CONF_PODS, CONF_REFRESH_TOKEN, DOMAIN
from custom_components.edistribuzione.coordinator import EdistribuzioneCoordinator

pytest_plugins = "pytest_homeassistant_custom_component"

POD_A = "IT001E10000001"
POD_B = "IT001E10000002"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    """Applied automatically to every test that uses the 'hass' fixture -
    except those that also require 'recorder_mock'.

    enable_custom_integrations depends on 'hass', so it forces it to be set
    up first. The recorder instead needs to apply its patches BEFORE 'hass'
    exists (see patch_recorder in the plugin): with both requested, 'hass'
    always wins the race and the recorder fails with "assert not
    hass_fixture_setup". The tests that use recorder_mock in this project call
    the module's functions directly (never async_setup_component or the config
    flow), so they do not need the custom integrations enabled: they are
    skipped cleanly, instead of reinventing the fixture order.
    """
    if "recorder_mock" in request.fixturenames:
        yield
        return
    request.getfixturevalue("enable_custom_integrations")
    yield


@pytest.fixture
def make_edist_coordinator(hass):
    """Factory: EdistribuzioneCoordinator with a MockConfigEntry attached to hass."""

    def _make(*, data=None, options=None, pods=None):
        pods = pods if pods is not None else [POD_A]
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_REFRESH_TOKEN: "rt", CONF_PODS: pods, **(data or {})},
            options=options or {},
        )
        entry.add_to_hass(hass)
        return EdistribuzioneCoordinator(hass, entry)

    return _make
