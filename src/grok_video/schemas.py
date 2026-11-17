from typing import Literal
from urllib.parse import urlsplit
import base64
import binascii
from ipaddress import ip_address

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class GrokVideoTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model_id: int = Field(gt=0, strict=True, description="Online myh_video_models.id")
    prompt: str | None = Field(default=None, min_length=1, max_length=30000)
    person_description: str | None = Field(default=None, min_length=1, max_length=2000)
    product_name: str | None = Field(default=None, min_length=1, max_length=500)
    script: str | None = Field(default=None, min_length=1, max_length=5000)
    display_mode: Literal["table", "handheld"] = "table"
    image_url: str | None = Field(default=None, min_length=1, max_length=14_000_000)
    product_image_url: str | None = Field(default=None, min_length=1, max_length=14_000_000)
    product_reference_image_urls: list[str] = Field(default_factory=list, max_length=5)
    image_model_id: int | None = Field(default=None, gt=0, strict=True)
    first_frame_mode: Literal["auto", "prompt"] | None = None
    first_frame_prompt: str | None = Field(default=None, min_length=1, max_length=10000)
    confirm_first_frame: bool = Field(default=False, strict=True)
    duration: int = Field(default=15, ge=1, le=15, strict=True)
    resolution: Literal["480p", "720p", "1080p"] = "720p"
    aspect_ratio: Literal["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"] = "9:16"
    n: Literal[1] = 1

    @field_validator("image_url", "product_image_url")
    @classmethod
    def validate_image_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.startswith("data:image/"):
            try:
                header, encoded = value.split(",", 1)
                raw = base64.b64decode(encoded, validate=True)
                signatures = {
                    "data:image/png;base64": raw.startswith(b"\x89PNG\r\n\x1a\n"),
                    "data:image/jpeg;base64": raw.startswith(b"\xff\xd8\xff"),
                    "data:image/webp;base64": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
                }
                if not signatures.get(header) or len(raw) > 10 * 1024 * 1024:
                    raise ValueError
            except (ValueError, binascii.Error):
                raise ValueError("Use base64 PNG/JPEG/WebP with matching signature, at most 10 MiB") from None
            return value
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError("image_url must be a public HTTP(S) URL")
        hostname = parsed.hostname.lower()
        if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
            raise ValueError("image_url must be public")
        try:
            address = ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("image_url must be public")
        return value

    @field_validator("product_reference_image_urls")
    @classmethod
    def validate_product_reference_image_urls(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("product_reference_image_urls must not contain duplicates")
        for value in values:
            cls.validate_image_url(value)
        return values

    @model_validator(mode="after")
    def template_or_prompt(self):
        if self.product_image_url is not None:
            if self.image_url is not None:
                raise ValueError("Choose product_image_url or an existing scene image_url")
            if self.image_model_id is None or self.first_frame_mode is None:
                raise ValueError("Product mode requires image_model_id and first_frame_mode")
            if self.first_frame_mode == "prompt" and self.first_frame_prompt is None:
                raise ValueError("Prompt mode requires first_frame_prompt")
            if self.first_frame_mode == "auto" and self.first_frame_prompt is not None:
                raise ValueError("Use prompt mode with first_frame_prompt")
        elif (self.image_url is None or self.image_model_id is not None
              or self.first_frame_mode is not None or self.first_frame_prompt is not None
              or self.confirm_first_frame):
            raise ValueError("Direct mode requires image_url without first-frame options")
        fields = (self.person_description, self.product_name, self.script)
        if self.prompt is not None and any(value is not None for value in fields):
            raise ValueError("Choose either prompt or template fields")
        if self.prompt is None and not all(fields):
            raise ValueError("Template mode requires person_description, product_name and script")
        return self


class GrokVideoTaskSubmitResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: int
    created_at: str


class GrokVideoTaskStatusResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: int
    provider_request_id: str | None = None
    error_message: str = ""
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    heartbeat_at: str | None = None
    first_frame: dict | None = None


class GrokVideoTaskResultResponse(BaseModel):
    task_id: str
    status: str
    result: dict
