import re
from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

# PowerOn emits an RFC-3339-style timestamp with whole seconds and either
# uppercase Z or a mandatory numeric offset.
DATE_GRAPH_PATTERN = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
GROUP_PATTERN = re.compile(r"[0-9]+\.[0-9]+")


def parse_date_graph(value: str | None) -> datetime | None:
    if value is None or DATE_GRAPH_PATTERN.fullmatch(value) is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


class GroupData(BaseModel):
    times: dict[str, str]


class BuildingGroup(BaseModel):
    cherg_gpv: StrictStr = Field(..., alias="chergGpv", min_length=1, max_length=32)

    @field_validator("cherg_gpv")
    @classmethod
    def normalize_group(cls, value: str) -> str:
        normalized = value.strip()
        if GROUP_PATTERN.fullmatch(normalized) is None:
            raise ValueError("group must use ASCII dotted numeric syntax")
        return normalized


class BuildingGroupsResponse(BaseModel):
    building_groups: list[BuildingGroup] = Field(
        ..., alias="buildingGroups", min_length=0, max_length=100
    )

    def normalized_groups(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.cherg_gpv for item in self.building_groups))

    def authoritative_group(self) -> str | None:
        groups = self.normalized_groups()
        return groups[0] if len(groups) == 1 else None


class ScheduleMember(BaseModel):
    # Members are intentionally tolerant here so one incomplete upstream member does
    # not hide other usable members. Usability is decided per event and group.
    id: StrictInt | StrictStr | None = None
    date_graph: str | None = Field(default=None, alias="dateGraph", strict=True)
    data_json: Any = Field(default_factory=dict, alias="dataJson")


class ScheduleResponse(BaseModel):
    events: list[ScheduleMember] = Field(..., alias="hydra:member")

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("events", mode="before")
    @classmethod
    def isolate_malformed_members(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value

        members: list[ScheduleMember] = []
        for raw_member in value:
            try:
                members.append(ScheduleMember.model_validate(raw_member))
            except ValidationError:
                # Retain an unusable placeholder so one malformed member cannot make
                # valid siblings disappear with a response-wide validation failure.
                members.append(ScheduleMember())
        return members

    @model_validator(mode="before")
    @classmethod
    def normalize_empty_members(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and "hydra:member" in data
            and data["hydra:member"] is None
            and type(data.get("hydra:totalItems")) is int
            and data["hydra:totalItems"] == 0
        ):
            return {**data, "hydra:member": []}
        return data
