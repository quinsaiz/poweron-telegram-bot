from pydantic import BaseModel, Field, ConfigDict
from typing import Dict, List


class GroupData(BaseModel):
    times: Dict[str, str]


class ScheduleMember(BaseModel):
    id: int
    date_graph: str = Field(..., alias="dateGraph")
    data_json: Dict[str, GroupData] = Field(..., alias="dataJson")


class ScheduleResponse(BaseModel):
    events: List[ScheduleMember] = Field(..., alias="hydra:member")

    model_config = ConfigDict(populate_by_name=True)
