"""Wire contract for local ML inference requests."""
from datetime import datetime, timezone, timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    device_id: str = Field(min_length=1, max_length=80,
                           pattern=r"^[a-zA-Z0-9_-]+$")
    window_start: datetime
    dataset: str = Field(min_length=1, max_length=40)
    participant_id: str = Field(min_length=1, max_length=40)
    sampling_hz: float = Field(gt=0, le=1000)
    acc_sampling_hz: float | None = Field(default=None, gt=0, le=1000)
    acc: list[list[float]] = Field(default_factory=list, max_length=20000)
    ppg: list[float] = Field(default_factory=list, max_length=20000)
    hr: list[float] = Field(default_factory=list, max_length=20000)

    @field_validator("window_start")
    @classmethod
    def aware(cls, value: datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("window_start must include a UTC offset")
        if value > datetime.now(timezone.utc) + timedelta(seconds=30):
            raise ValueError("window_start is in the future")
        return value.astimezone(timezone.utc)

    @field_validator("acc")
    @classmethod
    def three_axes(cls, rows):
        for row in rows:
            if len(row) != 3:
                raise ValueError("Each acc row must contain x, y, z")
        return rows

    @model_validator(mode="after")
    def online_channels(self):
        if self.dataset == "ppg-dalia" and self.hr:
            raise ValueError("PPG online input must not contain reference HR")
        return self
