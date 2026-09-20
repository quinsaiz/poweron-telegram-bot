from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GroupData(BaseModel):
    times: dict[str, str]


class ScheduleMember(BaseModel):
    id: int
    date_graph: str = Field(..., alias="dateGraph")
    data_json: dict[str, GroupData] = Field(..., alias="dataJson")


class ScheduleResponse(BaseModel):
    events: list[ScheduleMember] = Field(..., alias="hydra:member")

    model_config = ConfigDict(populate_by_name=True)

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
