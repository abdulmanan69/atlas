"""Pocket TTS and expressive voices: built-in voices, voices cloned from a consenting speaker,
and the sounds an expressive voice makes."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

CONSENT_ATTESTATION = "I have the speaker's permission to clone their voice."

CloningState = Literal["off", "enabling", "on", "failed", "unsupported"]
TokenSource = Literal["environment", "saved"]

CLONING_STATES = (
    "`off`: the server has no Hugging Face token, so voices cannot be cloned. `enabling`: the "
    "server is downloading and loading the voice-cloning weights. `on`: voices can be cloned. "
    "`failed`: the last attempt to turn it on failed; `message` says why. `unsupported`: the "
    "server is too old to turn voice cloning on or off from here; update it."
)
# Printable ASCII without spaces: what Hugging Face tokens are made of.
_TOKEN = re.compile(r"[\x21-\x7e]{20,200}")


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
    cloning_state: CloningState = Field(description=CLONING_STATES)
    setup_message: str | None = Field(
        default=None, description="What the server needs for voice cloning, when unavailable"
    )
    setup_url: str | None = Field(
        default=None, description="Where the gated voice-cloning weights' terms are accepted"
    )
    sound_tags: list[str] = Field(
        default_factory=list,
        description=(
            "Sounds the server's voices make when a reply contains them in square brackets, "
            "for example laugh for [laugh]. Empty for voices that cannot"
        ),
    )


class SoundTags(BaseModel):
    expressive: bool = Field(
        description=(
            "True when the connection's voice makes sounds written in square brackets, such "
            "as [laugh], instead of reading them"
        )
    )
    tags: list[str] = Field(description="Every sound the voice server lists, in its order")
    prompt_tags: list[str] = Field(
        description=(
            "The sounds agents with Natural sounds on are told they may use (at most four), "
            "most useful first"
        )
    )


class VoiceCloningStatus(BaseModel):
    state: CloningState = Field(description=CLONING_STATES)
    message: str | None = Field(
        default=None, description="What is happening or what went wrong, from the voice server"
    )
    terms_url: str = Field(
        description="Where a Hugging Face account accepts the terms of the voice-cloning weights"
    )
    token_source: TokenSource | None = Field(
        default=None,
        description=(
            "Where the server's Hugging Face token comes from: `environment` (set by whoever "
            "runs the server; it cannot be turned off here), `saved` (given through this API) "
            "or null (no token)."
        ),
    )
    can_manage: bool = Field(
        description="True when you may turn voice cloning on or off (organization owners)"
    )


class VoiceCloningEnable(BaseModel):
    hf_token: SecretStr = Field(
        min_length=20,
        max_length=200,
        description=(
            "A Hugging Face read token (it starts with hf_) of an account that accepted the "
            "terms of kyutai/pocket-tts. It is sent to the voice server, which keeps it to "
            "download the weights; this platform never stores, logs or returns it."
        ),
    )
    terms_accepted: bool = Field(
        default=False,
        description=(
            "Must be true: confirms that the token's Hugging Face account accepted the terms "
            "of kyutai/pocket-tts, which forbid cloning anyone's voice without their consent."
        ),
    )

    @field_validator("hf_token", mode="before")
    @classmethod
    def _token(cls, value: object) -> object:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str):
            return value
        raw = raw.strip()  # pasted tokens often carry a line break
        if not _TOKEN.fullmatch(raw):
            raise ValueError("Paste a Hugging Face token: 20 to 200 characters, no spaces")
        return raw
