"""WebSocket commands consumed by mmwave-card."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import API_VERSION, DEFAULT_FUSION_ID, DOMAIN, SIGNAL_UPDATE
from .coordinator import FusionCoordinator


def async_register_websocket_api(hass: HomeAssistant, coordinator: FusionCoordinator) -> None:
    hass.data[DOMAIN] = coordinator
    websocket_api.async_register_command(hass, ws_configure)
    websocket_api.async_register_command(hass, ws_get_config)
    websocket_api.async_register_command(hass, ws_remove_config)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_query_events)
    websocket_api.async_register_command(hass, ws_query_track)
    websocket_api.async_register_command(hass, ws_list_calibration_profiles)
    websocket_api.async_register_command(hass, ws_upsert_calibration_profile)
    websocket_api.async_register_command(hass, ws_remove_calibration_profile)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "mmwave_fusion/configure",
        vol.Required("config"): dict,
    }
)
@websocket_api.async_response
async def ws_configure(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    coordinator: FusionCoordinator = hass.data[DOMAIN]
    try:
        result = await coordinator.async_configure(msg["config"])
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_config", str(error))
        return
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "mmwave_fusion/get_config",
        vol.Optional("fusion_id", default=DEFAULT_FUSION_ID): str,
    }
)
@callback
def ws_get_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    coordinator: FusionCoordinator = hass.data[DOMAIN]
    fusion_id = msg["fusion_id"]
    connection.send_result(
        msg["id"],
        {
            # Lets the card detect a backend older than it needs and say so,
            # rather than failing on a command or field that does not exist yet.
            "api_version": API_VERSION,
            "config": coordinator.get_config(fusion_id),
            "status": coordinator.get_status(fusion_id),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "mmwave_fusion/remove_config",
        vol.Required("fusion_id"): str,
    }
)
@websocket_api.async_response
async def ws_remove_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    coordinator: FusionCoordinator = hass.data[DOMAIN]
    removed = await coordinator.async_remove(msg["fusion_id"])
    connection.send_result(msg["id"], {"removed": removed})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mmwave_fusion/subscribe",
        vol.Optional("fusion_id", default=DEFAULT_FUSION_ID): str,
    }
)
@callback
def ws_subscribe(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    fusion_id = msg["fusion_id"]

    @callback
    def forward(payload: dict[str, object]) -> None:
        connection.send_event(msg["id"], payload)

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(
        hass,
        f"{SIGNAL_UPDATE}_{fusion_id}",
        forward,
    )
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mmwave_fusion/query_events",
        vol.Optional("fusion_id", default=DEFAULT_FUSION_ID): str,
        vol.Optional("limit", default=100): vol.All(vol.Coerce(int), vol.Range(min=1, max=500)),
        vol.Optional("before"): vol.Coerce(float),
    }
)
@websocket_api.async_response
async def ws_query_events(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    coordinator: FusionCoordinator = hass.data[DOMAIN]
    result = await hass.async_add_executor_job(
        coordinator.trajectory_store.query_events,
        msg["fusion_id"],
        msg["limit"],
        msg.get("before"),
    )
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "mmwave_fusion/query_track",
        vol.Required("track_id"): str,
        vol.Optional("limit", default=5000): vol.All(vol.Coerce(int), vol.Range(min=1, max=20000)),
    }
)
@websocket_api.async_response
async def ws_query_track(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    coordinator: FusionCoordinator = hass.data[DOMAIN]
    result = await hass.async_add_executor_job(
        coordinator.trajectory_store.query_track,
        msg["track_id"],
        msg["limit"],
    )
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "mmwave_fusion/list_calibration_profiles",
    }
)
@callback
def ws_list_calibration_profiles(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    coordinator: FusionCoordinator = hass.data[DOMAIN]
    connection.send_result(msg["id"], coordinator.list_calibration_profiles())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "mmwave_fusion/upsert_calibration_profile",
        vol.Required("profile"): dict,
    }
)
@websocket_api.async_response
async def ws_upsert_calibration_profile(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    coordinator: FusionCoordinator = hass.data[DOMAIN]
    try:
        profile = await coordinator.async_upsert_calibration_profile(msg["profile"])
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_profile", str(error))
        return
    connection.send_result(msg["id"], profile)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "mmwave_fusion/remove_calibration_profile",
        vol.Required("profile_id"): str,
    }
)
@websocket_api.async_response
async def ws_remove_calibration_profile(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    coordinator: FusionCoordinator = hass.data[DOMAIN]
    removed = await coordinator.async_remove_calibration_profile(msg["profile_id"])
    connection.send_result(msg["id"], {"removed": removed})
