"""Natural sounds: an expressive voice laughs, sighs or gasps where the agent writes a cue.

An expressive voice server (Chatterbox-Turbo, infra/docker/expressive-tts) performs cues
such as `[laugh]` in the text as real sounds, drops bracketed text it does not know, and
lists its cues at GET {base}/audio/sound-tags (`voice_cloning.sound_tags`). Every
published agent gets exactly one rule about cues, after its own global rules
(`workflow_builder.global_rules_text`):

- the agent's voice (its own voice, else the organization's) is expressive and the agent's
  Natural sounds setting is on: it may add one cue per reply, from at most four of the
  sounds the server lists (`CUE_RULE`);
- otherwise: it writes no cues at all (`NO_CUES_RULE`), because any other voice would read
  them aloud. The Pocket TTS adapter also removes them, as a second line of defence.

The rule names the cues in a fixed order, so it is the same text on every call: the start
of the prompt does not change and stays in the language model's prompt cache. It is
decided when the agent is published (and for the prompt preview and the warm-up of a
self-hosted model); a later change of voice takes effect at the next publish.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.exceptions import NotFoundError
from app.schemas.voice import AgentConfig, ServiceChoice
from app.schemas.voice_cloning import SoundTags
from app.services import ai_settings, voice_cloning

log = logging.getLogger(__name__)

# The sounds that suit a phone conversation, most useful first; agents are told about the
# first MAX_PROMPT_TAGS of these that the server lists. Coughs, groans, sniffs and shushes
# are left out: they rarely help a call.
PREFERRED_TAGS = ("laugh", "chuckle", "sigh", "gasp", "clear throat")
MAX_PROMPT_TAGS = 4

CUE_RULE = (
    "You can add one sound cue in square brackets where a person would naturally make it: "
    "{cues}. Use at most one per reply, only when it fits the moment, never at the start, "
    "and never read or explain it."
)
NO_CUES_RULE = (
    "Write only the words to say aloud: never add sound cues, actions or notes in square "
    "brackets or between asterisks."
)


def prompt_tags(server_tags: Sequence[str]) -> list[str]:
    """The cues agents are told about: the preferred ones the server lists, in a fixed order."""
    listed = set(server_tags)
    return [t for t in PREFERRED_TAGS if t in listed][:MAX_PROMPT_TAGS]


def cue_rule(tags: Sequence[str]) -> str:
    return CUE_RULE.format(cues=", ".join(f"[{t}]" for t in tags))


def sound_rule(cfg: AgentConfig, server_tags: Sequence[str]) -> str:
    """The rule for an agent whose voice performs `server_tags` (empty: not expressive)."""
    tags = prompt_tags(server_tags)
    if cfg.call_settings.natural_sounds and tags:
        return cue_rule(tags)
    return NO_CUES_RULE


async def effective_tts(
    db: AsyncSession, org_id: uuid.UUID, cfg: AgentConfig
) -> ServiceChoice | None:
    """The voice the agent's calls use: its own, else the organization's."""
    override = cfg.model_override.tts if cfg.model_override else None
    if override is not None:
        return override
    return (await ai_settings.get_selection_for_org(db, org_id)).tts


async def agent_sound_tags(
    db: AsyncSession, org_id: uuid.UUID, cfg: AgentConfig
) -> tuple[str, ...]:
    """The sounds the agent's voice performs; empty when it is not an expressive voice."""
    choice = await effective_tts(db, org_id, cfg)
    if choice is None:
        return ()
    connections = await ai_settings.list_connections_for_org(db, org_id)
    conn = next((c for c in connections if c.id == choice.connection_id), None)
    if conn is None:
        return ()
    return await voice_cloning.sound_tags(db, org_id, conn)


async def agent_sound_rule(db: AsyncSession, org_id: uuid.UUID, cfg: AgentConfig) -> str:
    """The rule about cues for this agent. Never raises: when the voice cannot be
    determined, the agent is told not to write cues (they would otherwise be read aloud)."""
    if not cfg.call_settings.natural_sounds:
        return NO_CUES_RULE
    try:
        tags = await agent_sound_tags(db, org_id, cfg)
    except Exception as exc:
        log.info("natural_sounds_voice_unknown", extra={"error_type": type(exc).__name__})
        tags = ()
    return sound_rule(cfg, tags)


async def connection_sound_tags(
    db: AsyncSession, ctx: TenantContext, connection_id: str
) -> SoundTags:
    """Whether a voice connection is expressive, and the sounds it makes."""
    for conn in await ai_settings.list_connections(db, ctx):
        if conn.id == connection_id:
            tags = list(await voice_cloning.sound_tags(db, ctx.org_id, conn))
            return SoundTags(expressive=bool(tags), tags=tags, prompt_tags=prompt_tags(tags))
    raise NotFoundError("Connection not found")
