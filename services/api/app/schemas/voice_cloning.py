"""Pocket TTS and expressive voices: built-in voices, voices cloned from a consenting speaker,
voices designed from a description, what a voice server can do, and the sounds an expressive
voice makes."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

CONSENT_ATTESTATION = "I have the speaker's permission to clone their voice."

CloningState = Literal["off", "enabling", "on", "failed", "unsupported"]
TokenSource = Literal["environment", "saved"]
FindingLevel = Literal["ok", "warning", "problem"]

CLONING_STATES = (
    "`off`: the server has no Hugging Face token, so voices cannot be cloned. `enabling`: the "
    "server is downloading and loading the voice-cloning weights. `on`: voices can be cloned. "
    "`failed`: the last attempt to turn it on failed; `message` says why. `unsupported`: the "
    "server is too old to turn voice cloning on or off from here; update it."
)
# Printable ASCII without spaces: what Hugging Face tokens are made of.
_TOKEN = re.compile(r"[\x21-\x7e]{20,200}")


class QualityFinding(BaseModel):
    level: FindingLevel = Field(
        description=(
            "`problem`: the recording is refused for cloning. `warning`: the clone may suffer, "
            "but the voice is created. `ok`: a note, nothing to do."
        )
    )
    code: str = Field(
        description=(
            "A stable name for what was found: `clipping`, `silent`, `noise`, `little_speech`, "
            "`quiet`, `uneven_level`, `silence_start`, `silence_end`, `long_pause`, "
            "`speech_ratio`, `short`, `dc_offset`, `low_sample_rate`, `stereo`, `compressed` "
            "or `window` (which part of a longer upload is used)"
        )
    )
    message: str = Field(description="What was found and what to do about it")


class SampleQuality(BaseModel):
    score: int = Field(
        ge=0,
        le=100,
        description=(
            "Overall mark for the part of the recording that is cloned from: 100 with "
            "nothing to report; each warning takes 12 points off, each problem 35"
        ),
    )
    findings: list[QualityFinding] = Field(
        description="Most serious first; empty for a recording with nothing to report"
    )


class SampleCheck(SampleQuality):
    seconds: float = Field(
        description=(
            "Length of the part that is cloned from: the upload without the silence at its "
            "ends, or from a longer upload the 10 to 15 seconds with the most speech"
        )
    )
    start: float = Field(description="Where that part starts, in seconds into the upload")
    upload_seconds: float = Field(description="Length of the whole upload")


class PocketVoice(BaseModel):
    id: str = Field(description="The value to use as the voice setting")
    name: str
    cloned: bool = Field(description="True for voices cloned by this organization")
    license: str | None = Field(default=None, description="Recording license (built-in voices)")
    default: bool = False
    created_at: datetime | None = None
    created_by: uuid.UUID | None = None
    sample_seconds: float | None = None
    quality: SampleQuality | None = Field(
        default=None,
        description=(
            "How good the reference recording was, for a voice just cloned (`warning` and "
            "`ok` findings only: a `problem` refuses the clone). Not in voice lists."
        ),
    )
    designed: bool = Field(
        default=False, description="True for voices this organization designed from a description"
    )
    description: str | None = Field(
        default=None, description="The description a designed voice was made from"
    )


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


Streaming = Literal["sentence", "chunk", "none", "unknown"]


class ReferenceSeconds(BaseModel):
    min: int = Field(ge=0, description="Shortest sample voice cloning accepts")
    max: int = Field(ge=0, description="Longest sample voice cloning accepts")


class VoiceCapabilities(BaseModel):
    """What a voice server can do, as it states at GET /v1/capabilities; derived for an
    older server without that endpoint (then `design` and `instruct` are false and
    `streaming` is unknown)."""

    model: str | None = Field(default=None, description="The server's model, as it names it")
    cloning: bool = Field(description="Clones a voice from a recording of a consenting speaker")
    design: bool = Field(description="Designs a new voice from a written description")
    instruct: bool = Field(
        description="Takes delivery instructions (tone, pace) along with the words to say"
    )
    sound_tags: list[str] = Field(
        description="Sounds its voices make for cues in square brackets, such as [laugh]"
    )
    languages: list[str] = Field(description="Languages it speaks, as the server names them")
    streaming: Streaming = Field(
        description=(
            "How it delivers speech: `sentence` by sentence, in `chunk`s, `none` (the whole "
            "reply at once) or `unknown` (an older server)"
        )
    )
    reference_seconds: ReferenceSeconds | None = Field(
        default=None, description="Sample length voice cloning accepts"
    )


class VoiceDesignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="Shown in voice pickers")
    description: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "The voice in plain words, 10 to 300 characters: who is speaking and how, for "
            "example age, gender, accent, warmth, pace. Not a real person's name."
        ),
    )
    language: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "One of the server's `languages` (GET .../capabilities); its default when empty"
        ),
    )
    sample_text: str | None = Field(
        default=None,
        max_length=300,
        description="What the preview says; the server's own line when empty",
    )
    seed: int | None = Field(
        default=None,
        ge=0,
        le=2**31 - 1,
        description=(
            "Makes the design repeatable: the same description and seed give the same voice"
        ),
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
