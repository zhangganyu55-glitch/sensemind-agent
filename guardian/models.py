"""Sensor/event contracts. All measurements are non-clinical prototype data."""
from datetime import datetime, timedelta, timezone
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class DeviceStatus(BaseModel):
    transport: Literal["stlink"]
    sampling_status: Literal["pending", "live", "stalled", "disconnected"]
    sample_counter: int | None = Field(default=None, ge=0, le=0xffffffff)
    finger_detected: bool | None = None
    firmware_heart_rate_valid: bool | None = None
    firmware_spo2_valid: bool | None = None
    signal_weak: bool | None = None
    fifo_overflow_count: int | None = Field(default=None, ge=0, le=0xffffffff)
    spo2_method: Literal["firmware_estimate"] = "firmware_estimate"


class Telemetry(BaseModel):
    device_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: Literal["simulator", "stm32", "ring", "esp32"] = "simulator"
    data_mode: Literal["live", "replay"] | None = None
    heart_rate: float | None = Field(default=None, ge=0, le=250)
    spo2: float | None = Field(default=None, ge=0, le=100)
    acc_x: float | None = Field(default=None, ge=-32, le=32)
    acc_y: float | None = Field(default=None, ge=-32, le=32)
    acc_z: float | None = Field(default=None, ge=-32, le=32)
    gyro_x: float | None = None
    gyro_y: float | None = None
    gyro_z: float | None = None
    signal_quality: Literal["good", "poor", "unknown"] = "unknown"
    reading_valid: bool = False
    device_status: DeviceStatus | None = None
    # Optional provenance for dataset replay; never inferred from the device.
    dataset: str | None = Field(default=None, max_length=40)
    participant_id: str | None = Field(default=None, max_length=40)
    sampling_hz: float | None = Field(default=None, gt=0, le=1000)

    @model_validator(mode="after")
    def source_mode(self):
        inferred = "replay" if self.source == "simulator" or self.dataset else "live"
        if self.data_mode is not None and self.data_mode != inferred:
            raise ValueError("data_mode conflicts with source/dataset")
        self.data_mode = inferred
        return self

    @field_validator("timestamp")
    @classmethod
    def must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must have explicit timezone")
        value = value.astimezone(timezone.utc)
        if value > datetime.now(timezone.utc) + timedelta(seconds=30):
            raise ValueError("timestamp is more than 30 seconds in the future; check device clock")
        return value


class AskRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    question: str = Field(min_length=1, max_length=500)


class ToolCall(BaseModel):
    device_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    tool: Literal["get_latest", "get_history", "list_events", "summarize_status", "get_model_outputs"]
    limit: int = Field(default=20, ge=1, le=200)
