"""Opt-in Home Assistant LLM API over compact fusion queries."""

# ruff: noqa: RUF001

from __future__ import annotations

import logging
import time
from datetime import UTC, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from .const import DOMAIN
from .coordinator import FusionCoordinator
from .query import (
    QueryError,
    clamp_limit,
    compact_event,
    diagnose_radar,
    format_local_time,
    live_occupancy,
    normalize_event_type,
    resolve_fusion_id,
    resolve_zone,
    summarize_heatmap,
    tally_event_counts,
    window_from_args,
    zone_names,
)

_LOGGER = logging.getLogger(__name__)

try:
    import probatio as _schema_mod
except ImportError:
    _schema_mod = vol

_Optional = _schema_mod.Optional
_Schema = _schema_mod.Schema

_READ_ONLY = getattr(llm, "ToolAnnotations", None)
_ANNOTATIONS = (
    _READ_ONLY(read_only=True, destructive=False, idempotent=True, open_world=False)
    if _READ_ONLY is not None
    else None
)

PROMPT_EN = """\
You query anonymous mmWave radar fusion for this home.

Rules:
- Never name a person. Say "someone" or "a person". track_id is ephemeral and can break.
- Coordinates are centimetres in the room frame, origin at the configured corner.
- No frames yet is not an empty room. Say so if ready is false.
- GetOccupancy is live. QueryEvents is history. SummarizePresence is where people lingered.
- DiagnoseCalibration: mirrored motion means yaw is 180° off; motion at right angles means 90° off. 2D radars cannot infer pitch/roll from floor points. Send the user to the mmWave card's calibration tabs rather than inventing numbers.
"""

PROMPT_ZH = """\
你在查询这个家里匿名的毫米波雷达融合数据。

规则：
- 不要用人名。只能说「有人」或「一条轨迹」。track_id 会断、会换。
- 坐标是房间坐标系，单位厘米，原点是配置的墙角。
- 还没有数据帧并不等于房间是空的。ready 为 false 时要说清楚。
- GetOccupancy 看现在；QueryEvents 看历史；SummarizePresence 看人常在哪。
- DiagnoseCalibration：点镜像说明偏航角差 180°；点和移动方向成直角说明差 90°。二维雷达不能从平面参考点反推俯仰/横滚。让用户回 mmWave 卡片的校准页，不要随口改数字。
"""


def async_register_llm_api(hass: HomeAssistant) -> Any:
    try:
        unregister = llm.async_register_api(
            hass,
            FusionLLMAPI(hass=hass, id=DOMAIN, name="MMWave Fusion"),
        )
    except (AttributeError, TypeError, HomeAssistantError) as error:
        # helpers.llm is present on current Home Assistant, but older cores and
        # a missing conversation setup should not keep fusion from loading.
        _LOGGER.warning(
            "Home Assistant LLM API is unavailable; fusion tools were not registered: %s",
            error,
        )
        return lambda: None
    return unregister


class FusionLLMAPI(llm.API):
    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        language = llm_context.language or ""
        prompt = PROMPT_ZH if language.lower().startswith("zh") else PROMPT_EN
        return llm.APIInstance(
            api=self,
            api_prompt=prompt,
            llm_context=llm_context,
            tools=[
                GetOccupancyTool(),
                QueryEventsTool(),
                SummarizePresenceTool(),
                DiagnoseCalibrationTool(),
            ],
        )


class _FusionTool(llm.Tool):
    integration = DOMAIN
    if _ANNOTATIONS is not None:
        annotations = _ANNOTATIONS


def _coordinator(hass: HomeAssistant) -> FusionCoordinator:
    coordinator = hass.data.get(DOMAIN)
    if coordinator is None:
        raise HomeAssistantError("MMWave Fusion is not set up")
    return coordinator


def _tzinfo(hass: HomeAssistant) -> tzinfo:
    name = getattr(hass.config, "time_zone", None) or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError, TypeError):
        return UTC


def _system(coordinator: FusionCoordinator, fusion_id: str | None):
    resolved = resolve_fusion_id(list(coordinator.systems), fusion_id)
    system = coordinator.systems[resolved]
    return resolved, system


class GetOccupancyTool(_FusionTool):
    name = "GetOccupancy"
    description = (
        "Live occupancy for a fusion system: how many anonymous tracks are in "
        "the room and in each named zone right now. Optional fusion_id."
    )
    parameters = _Schema(
        {_Optional("fusion_id", description="Fusion system id. Omit if only one exists."): str}
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        try:
            coordinator = _coordinator(hass)
            fusion_id, system = _system(coordinator, tool_input.tool_args.get("fusion_id"))
        except QueryError as error:
            raise HomeAssistantError(str(error)) from error
        status = system.status()
        return live_occupancy(
            fusion_id=fusion_id,
            ready=bool(status.get("ready")),
            target_count=int(status.get("target_count") or 0),
            zone_occupancy=dict(status.get("zone_occupancy") or {}),
            zones=list(system.config.get("zones") or []),
            radar_health=system.radar_health(),
        )


class QueryEventsTool(_FusionTool):
    name = "QueryEvents"
    description = (
        "List recent anonymous zone events (enter, exit, dwell, traverse, trajectory). "
        "Optional: fusion_id, hours (default 24, max 168), since, until (ISO local time), "
        "zone (id or name), event_type, limit (max 50)."
    )
    parameters = _Schema(
        {
            _Optional("fusion_id"): str,
            _Optional("hours", description="Lookback in hours, default 24, max 168."): float,
            _Optional("since", description="ISO local start time."): str,
            _Optional("until", description="ISO local end time."): str,
            _Optional("zone", description="Zone id or display name."): str,
            _Optional("event_type"): str,
            _Optional("limit"): int,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        try:
            coordinator = _coordinator(hass)
            args = tool_input.tool_args
            fusion_id, system = _system(coordinator, args.get("fusion_id"))
            tzinfo = _tzinfo(hass)
            since, until = window_from_args(
                time.time(),
                hours=args.get("hours"),
                since=args.get("since"),
                until=args.get("until"),
                tzinfo=tzinfo,
            )
            zone_id = resolve_zone(list(system.config.get("zones") or []), args.get("zone"))
            event_type = normalize_event_type(args.get("event_type"))
            limit = clamp_limit(args.get("limit"))
        except QueryError as error:
            raise HomeAssistantError(str(error)) from error
        rows = await hass.async_add_executor_job(
            coordinator.trajectory_store.query_events,
            fusion_id,
            limit,
            None,
            since,
            until,
            zone_id,
            event_type,
        )
        names = zone_names(list(system.config.get("zones") or []))
        return {
            "fusion_id": fusion_id,
            "events": [compact_event(row, names, tzinfo) for row in rows],
        }


class SummarizePresenceTool(_FusionTool):
    name = "SummarizePresence"
    description = (
        "Where people lingered in a time window: event counts by zone and the "
        "hottest occupancy-grid cells. Optional: fusion_id, hours (default 24), "
        "since, until, zone."
    )
    parameters = _Schema(
        {
            _Optional("fusion_id"): str,
            _Optional("hours"): float,
            _Optional("since"): str,
            _Optional("until"): str,
            _Optional("zone"): str,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        try:
            coordinator = _coordinator(hass)
            args = tool_input.tool_args
            fusion_id, system = _system(coordinator, args.get("fusion_id"))
            tzinfo = _tzinfo(hass)
            since, until = window_from_args(
                time.time(),
                hours=args.get("hours"),
                since=args.get("since"),
                until=args.get("until"),
                tzinfo=tzinfo,
            )
            zone_id = resolve_zone(list(system.config.get("zones") or []), args.get("zone"))
        except QueryError as error:
            raise HomeAssistantError(str(error)) from error
        store = coordinator.trajectory_store
        counts = await hass.async_add_executor_job(
            store.event_counts, fusion_id, since, until, zone_id
        )
        grid = await hass.async_add_executor_job(
            store.occupancy_grid, fusion_id, since, until, 20.0
        )
        names = zone_names(list(system.config.get("zones") or []))
        return {
            "fusion_id": fusion_id,
            "from": format_local_time(since, tzinfo),
            "until": format_local_time(until, tzinfo),
            "counts": tally_event_counts(counts, names),
            "heatmap": summarize_heatmap(grid),
        }


class DiagnoseCalibrationTool(_FusionTool):
    name = "DiagnoseCalibration"
    description = (
        "Explain whether each radar looks miscalibrated: not reporting, stale frames, "
        "or detections landing outside the room. Optional fusion_id."
    )
    parameters = _Schema({_Optional("fusion_id"): str})

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        try:
            coordinator = _coordinator(hass)
            fusion_id, system = _system(coordinator, tool_input.tool_args.get("fusion_id"))
        except QueryError as error:
            raise HomeAssistantError(str(error)) from error
        return {
            "fusion_id": fusion_id,
            "radars": [diagnose_radar(item) for item in system.radar_health()],
        }


