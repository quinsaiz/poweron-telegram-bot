from pydantic import BaseModel, ConfigDict, Field


class GroupData(BaseModel):
    times: dict[str, str]


class ScheduleMember(BaseModel):
    id: int
    date_graph: str = Field(..., alias="dateGraph")
    data_json: dict[str, GroupData] = Field(..., alias="dataJson")


class ScheduleResponse(BaseModel):
    events: list[ScheduleMember] = Field(..., alias="hydra:member")

    model_config = ConfigDict(populate_by_name=True)
