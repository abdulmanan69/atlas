"""Pocket TTS voices: built-in voices and voices cloned from a consenting speaker."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

CONSENT_ATTESTATION = "I have the speaker's permission to clone their voice."


class PocketVoice(BaseModel):
    id: str = Field(description="The value to use as the voice setting")
    name: str
    cloned: bool = Field(description="True for voices cloned by this organization")
    license: str | None = Field(default=None, description="Recording license (built-in voices)")
    default: bool = False
    created_at: datetime | None = None
    created_by: uuid.UUID | None = None
    sample_seconds: float | None = None


class PocketVoiceList(BaseModel):
    voices: list[PocketVoice]
    cloning_available: bool = Field(
        description="False when the Pocket TTS server has no voice-cloning weights"
    )
    setup_message: str | None = Field(
        default=None, description="What the server needs for voice cloning, when unavailable"
    )
    setup_url: str | None = Field(
        default=None, description="Where the gated voice-cloning weights' terms are accepted"
    )
