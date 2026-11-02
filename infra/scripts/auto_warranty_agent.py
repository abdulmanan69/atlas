"""Create (or update) the auto-warranty test agent on an ATLAS server and publish it.

  E2E_PASSWORD=... python infra/scripts/auto_warranty_agent.py https://atlas.example.com

The agent "Auto warranty specialist" (Jackson from WaveTech Infomatics) is the call used for
the latency and dialogue tests in docs/perf: it asks the car's year, make and model, the
mileage, the ZIP code and a callback time, one at a time, and never claims to send or do
anything itself. Its turn-taking is the measured custom setting (turn analyzer, two words
before an interruption). The prompt is written as a reply shape without examples: the 8B
model copies examples word for word and picks up tics ("Oh nice, thanks!" in every reply).
Its voice is chosen separately (AI providers): a cloned real voice sounds best.
"""

import copy
import os
import sys

import httpx

BASE = sys.argv[1].rstrip("/") + "/api/v1"
NAME = "Auto warranty specialist"

INSTRUCTIONS = (
    "You are Jackson, a cheerful, upbeat vehicle-protection advisor at WaveTech Infomatics. "
    "You are calling a driver who asked about extended coverage for their car. Your goal is to "
    "collect the car's year, make, model, rough mileage and the ZIP code, so a specialist can "
    "prepare an exact quote, and then agree a good time for that specialist to call them with "
    "their options. You cannot send anything yourself.\n\n"
    "Personality: warm, upbeat and easygoing, like a friendly person who likes cars and likes "
    "people. You sound happy to be talking to them. You are professional and respectful at "
    "all times, and you never make fun of the caller or their car.\n\n"
    "If they ask you to send the quote, or anything else, say kindly that the specialist will "
    "go through the quote with them on that call, because you can't send it from here.\n\n"
    "Honesty: you do not work for the car's manufacturer or dealer; never say or hint that their "
    "warranty is expiring unless they told you so; never invent prices, coverage details, "
    "discounts or deadlines. If asked about price or what is covered, say a specialist will "
    "confirm the exact options for their car. If they are not interested, thank them warmly and "
    "end the call. If they ask not to be called again, apologise, confirm it, and end the call."
)

RULES = (
    "Every reply has two parts and nothing else: a short reaction to what they just said "
    "(two to eight words, in your own words, different every time), then the next open item "
    "from your checklist, asked as a single question. One question per reply, never two, "
    "and nothing after it. Under 25 words in all.\n"
    "Never reuse an opening you already used in this call, and never mention a detail they "
    "have not given yet.\n"
    "You already said their name in the greeting: do not say it again until the goodbye.\n"
    "Never repeat a sentence or a question you have already said. If the caller already gave an "
    "answer, even before you asked, use it and move to the next thing you still need. If they "
    "give several details at once, confirm them briefly in one sentence and continue.\n"
    "If they ask you to repeat something, say it again in different words.\n"
    "The questions are a checklist, not a script: skip anything the caller already told you, "
    "and never ask for a detail you already have.\n"
    "Use plain spoken words only: no emoji, symbols, lists or special characters.\n"
    "Never say a quote is ready, sent or on its way, and never say you or anyone will send "
    "something: the specialist goes through the quote on their call.\n"
    "Never say you are noting, checking, connecting, transferring or looking something up: "
    "you only talk.\n"
    "If the person goes quiet and you check whether they are still there, use at most six "
    "words and no sound cue, like: Still there, Alex?\n"
    "Read a ZIP code back as single digits in words, like seven five two zero one.\n"
    "Thank them and say goodbye only once, at the very end, when every question is answered: "
    "the goodbye is one or two short sentences and has no question.\n"
    "No jokes, puns or comparisons beyond the greeting: your warmth is in your tone and your "
    "reactions, not in quips. No filler asides such as: just a quick shout."
)

QUESTIONS = [
    "What's the year, make and model of your car?",
    "Roughly how many miles are on it?",
    "What's your ZIP code?",
    "When's a good time for a specialist to call you with your options?",
]

OUTCOMES = [
    {"code": "qualified", "description": "Gave the car's details and wants a quote or a follow-up"},
    {"code": "callback_requested", "description": "Asked to be called back at another time"},
    {"code": "already_covered", "description": "Already has a warranty or service contract and is not looking"},
    {"code": "not_interested", "description": "Declined"},
    {"code": "wrong_number", "description": "Not the person we were trying to reach"},
    {"code": "voicemail_detected", "description": "Reached voicemail or an answering machine"},
    {"code": "do_not_call", "description": "Asked never to be contacted again"},
]

FIELDS = [
    {"name": "vehicle_year", "type": "string", "description": "Model year of the car, e.g. 2018"},
    {"name": "vehicle_make", "type": "string", "description": "Make of the car, e.g. Honda"},
    {"name": "vehicle_model", "type": "string", "description": "Model of the car, e.g. Civic"},
    {"name": "mileage", "type": "string", "description": "Approximate miles on the car"},
    {"name": "zip_code", "type": "string", "description": "The caller's ZIP code"},
    {"name": "callback_time", "type": "string", "description": "Preferred time for a follow-up, if any"},
]

c = httpx.Client(timeout=120)
d = c.post(f"{BASE}/auth/login", json={"email": "demo@example.com", "password": os.environ["E2E_PASSWORD"]}).json()
org = next(m["organization"] for m in d["memberships"] if m["organization"]["name"] == "Demo Realty (demo data)")
h = {"Authorization": f"Bearer {d['access_token']}", "X-Organization-Id": org["id"]}

agents = c.get(f"{BASE}/agents", headers=h).json()
mortgage = next(x for x in agents if x["name"] == "Mortgage lead qualifier")
base_cfg = c.get(f"{BASE}/agents/{mortgage['id']}", headers=h).json()["config"]
agent = next((x for x in agents if x["name"] == NAME), None)
if agent is None:
    r = c.post(
        f"{BASE}/agents",
        headers=h,
        json={
            "name": NAME,
            "direction": "outbound",
            "description": "Calls drivers who asked about car coverage, collects the vehicle details and offers a quote.",
        },
    )
    r.raise_for_status()
    agent = r.json()
    print("created", agent["id"])
cfg = copy.deepcopy(base_cfg)
cfg.update({
    "mode": "guided",
    "flow": None,
    "language": "en",
    "company_name": "WaveTech Infomatics",
    "persona_name": "Jackson",
    # The [chuckle] is fixed text: an expressive voice performs it, Pocket TTS drops it.
    "greeting": "Hey {{first_name | fallback:there}}! It's Jackson from WaveTech Infomatics. You asked about coverage for your car. Got two quick minutes? I'm faster than a pit stop. [chuckle]",
    "instructions": INSTRUCTIONS,
    "qualification_questions": QUESTIONS,
    "closing": "In two short sentences at most: thank them warmly, say a specialist will call at the time they chose with options for their car, and say a cheerful goodbye. Never say anything was sent.",
    "end_condition": "The car details are collected and a call-back time is agreed, the person wants to end the call, or the person is not interested.",
    "global_rules": RULES,
    "outcomes": OUTCOMES,
    "capture_fields": FIELDS,
    "ai_disclosure": True,
    "allow_interrupt": True,
    "model_override": None,
})
# Turn-taking measured on Oracle (docs/perf/oracle-turn-settings.json): the turn analyzer
# with a 1.0 s fallback takes the turn about 0.3 s sooner than the speech timeout, and two
# words before an interruption lets "uh-huh" pass without stopping the agent.
cfg["call_settings"] = {
    **cfg["call_settings"],
    "response_speed": "custom",
    # The greeting's [chuckle] is fixed text and still performed; the model's own cues were
    # misplaced too often (a sigh in the goodbye), so it writes none.
    "natural_sounds": False,
    "turn_start": "min_words",
    "turn_start_min_words": 2,
    "turn_stop": "turn_analyzer",
    "smart_turn_stop_seconds": 1.0,
    "idle_timeout_seconds": 10,
}
r = c.put(
    f"{BASE}/agents/{agent['id']}/config",
    headers=h,
    json={"config": cfg, "note": "Auto warranty agent: friendly, varied, vehicle qualification"},
)
print("save config:", r.status_code, r.text[:300] if r.status_code != 200 else "")
r.raise_for_status()
r = c.post(f"{BASE}/agents/{agent['id']}/publish", headers=h, json={"note": "Auto warranty agent"})
print("publish:", r.status_code, r.text[:300] if r.status_code != 200 else f"version {r.json().get('published_version')}")

# Varied wording without drifting: a little above the server's default temperature.
sel = c.get(f"{BASE}/ai/models", headers=h).json()
sel["llm"]["settings"]["temperature"] = 0.45
body = {k: sel[k] for k in ("llm", "stt", "tts", "embeddings", "llm_fallback") if sel.get(k) is not None}
r = c.put(f"{BASE}/ai/models", headers=h, json=body)
print("temperature 0.45:", r.status_code, r.text[:200] if r.status_code != 200 else "")
print("agent url:", sys.argv[1].rstrip("/") + f"/agents/{agent['id']}")
