import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import { CloneVoiceDialog } from "@/features/voice/clone-voice-dialog";
import { PocketVoicePicker } from "@/features/voice/pocket-voice-picker";
import { isPocketConnection } from "@/features/voice/voice-cloning";
import type * as voiceSample from "@/features/voice/voice-sample";
import { encodeWav, MAX_SAMPLE_BYTES, toVoiceSample, validateClone, validateSampleFile, type VoiceSample } from "@/features/voice/voice-sample";
import { session } from "@/lib/session";

// jsdom has no Web Audio: the conversion to WAV is replaced, everything else is real.
vi.mock("@/features/voice/voice-sample", async (importOriginal) => {
  const real = await importOriginal<typeof voiceSample>();
  return {
    ...real,
    toVoiceSample: vi.fn(async (_blob: Blob, source: VoiceSample["source"], label: string): Promise<VoiceSample> => ({ wav: real.encodeWav(new Float32Array(24000 * 6), 24000), seconds: 6, source, label })),
  };
});

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const SETUP = "Voice cloning needs the gated weights: accept the terms at https://huggingface.co/kyutai/pocket-tts and set HF_TOKEN.";
const CHECK = "POST /api/v1/ai/connections/c1/voices/check";
const CLEAN = { score: 100, findings: [], seconds: 6, start: 0, upload_seconds: 6 };
const CLIPPED = {
  score: 41,
  seconds: 5.9,
  start: 0,
  upload_seconds: 6,
  findings: [
    { level: "problem", code: "clipping", message: "The recording clips in 2.1 % of samples: lower the input gain or move back from the microphone, and record again." },
    { level: "warning", code: "short", message: "Short (5.9 s): 10 to 20 seconds of speech gives a better clone." },
    { level: "ok", code: "stereo", message: "Stereo: the two channels were mixed to one." },
  ],
};

function renderWithProviders(ui: React.ReactElement) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  session.getState().signIn({
    access_token: "t",
    token_type: "bearer",
    expires_in: 900,
    user: { id: "u1", email: "a@example.com", full_name: "Ada", created_at: "2026-01-01T00:00:00Z" },
    memberships: [{ id: "m1", role: "admin", status: "active", organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "GB", created_at: "2026-01-01T00:00:00Z" } }],
  } as never);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(toVoiceSample).mockClear();
});

const sample = (seconds: number): VoiceSample => ({ wav: new Blob(["x"]), seconds, source: "upload", label: "a.wav" });

const TERMS = "https://huggingface.co/kyutai/pocket-tts";
const status = (state: string, extra: Record<string, unknown> = {}) => ({ state, message: null, terms_url: TERMS, token_source: null, can_manage: true, ...extra });

/** Answers by method and path; records every request. */
function route(handlers: Record<string, (req: Request) => Response | Promise<Response>>) {
  const calls: Request[] = [];
  const fetchMock = vi.fn(async (req: Request) => {
    calls.push(req);
    const key = `${req.method} ${new URL(req.url).pathname}`;
    const handler = handlers[key];
    if (!handler) throw new Error(`unexpected request ${key}`);
    return handler(req);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

function signInAs(role: "owner" | "admin") {
  session.getState().signIn({
    access_token: "t",
    token_type: "bearer",
    expires_in: 900,
    user: { id: "u1", email: "a@example.com", full_name: "Ada", created_at: "2026-01-01T00:00:00Z" },
    memberships: [{ id: "m1", role, status: "active", organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "GB", created_at: "2026-01-01T00:00:00Z" } }],
  } as never);
}

describe("clone form validation", () => {
  it("requires a name, a 5-75 s sample and the speaker's permission", () => {
    expect(validateClone({ name: " ", consent: false, sample: null })).toEqual({
      name: "Give the voice a name.",
      sample: "Add a voice sample: upload a file or record one.",
      consent: "Confirm that the speaker gave permission before cloning their voice.",
    });
    expect(validateClone({ name: "x".repeat(61), consent: true, sample: sample(6) }).name).toMatch(/at most 60/);
    expect(validateClone({ name: "Sam", consent: true, sample: sample(4.9) }).sample).toMatch(/at least 5 seconds/);
    expect(validateClone({ name: "Sam", consent: true, sample: sample(75.5) }).sample).toMatch(/at most 75 seconds/);
    expect(validateClone({ name: "Sam", consent: true, sample: sample(6) })).toEqual({});
    expect(validateClone({ name: "Sam", consent: true, sample: sample(40) })).toEqual({});
  });

  it("accepts WAV, MP3, M4A, OGG, WebM and FLAC files up to 10 MB", () => {
    expect(validateSampleFile(new File(["x"], "sam.wav"))).toBeNull();
    expect(validateSampleFile(new File(["x"], "sam.MP3"))).toBeNull();
    for (const name of ["sam.ogg", "sam.webm", "sam.flac"]) expect(validateSampleFile(new File(["x"], name))).toBeNull();
    expect(validateSampleFile(new File(["x"], "memo", { type: "audio/mp4" }))).toBeNull();
    expect(validateSampleFile(new File(["x"], "setup.exe"))).toMatch(/isn't a supported type/);
    expect(validateSampleFile(new File([], "empty.wav"))).toMatch(/is empty/);
    expect(validateSampleFile(new File([new Uint8Array(MAX_SAMPLE_BYTES + 1)], "big.m4a"))).toMatch(/larger than 10 MB/);
  });

  it("encodes 16-bit mono WAV", async () => {
    const wav = encodeWav(new Float32Array([0, 1, -1, 2]), 24000);
    // jsdom's Blob has no arrayBuffer(); FileReader reads it the same way.
    const bytes = await new Promise<ArrayBuffer>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.readAsArrayBuffer(wav);
    });
    const view = new DataView(bytes);
    const text = (o: number) => String.fromCharCode(...new Uint8Array(view.buffer, o, 4));
    expect([text(0), text(8), text(12), text(36)]).toEqual(["RIFF", "WAVE", "fmt ", "data"]);
    expect(view.getUint32(24, true)).toBe(24000);
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(40, true)).toBe(8);
    expect([view.getInt16(46, true), view.getInt16(48, true), view.getInt16(50, true)]).toEqual([32767, -32768, 32767]);
  });

  it("recognises Pocket TTS connections like the API does", () => {
    const base = { id: "c", provider: "speaches", provider_title: "Self-hosted", configured_credentials: [], hosted_trial: false };
    expect(isPocketConnection({ ...base, name: "Pocket TTS", connection_settings: {} })).toBe(true);
    expect(isPocketConnection({ ...base, name: "Voice", connection_settings: { base_url: "http://local-tts:8000/v1" } })).toBe(true);
    expect(isPocketConnection({ ...base, name: "Local speech", connection_settings: { base_url: "http://local-speech:8000/v1" } })).toBe(false);
    expect(isPocketConnection({ ...base, provider: "openai", name: "pocket", connection_settings: {} })).toBe(false);
    // The expressive voice server has the same voice API, by name or address, or behind an AI server.
    expect(isPocketConnection({ ...base, name: "Expressive voice", connection_settings: {} })).toBe(true);
    expect(isPocketConnection({ ...base, name: "GPU", connection_settings: { base_url: "http://chatterbox:8000/v1" } })).toBe(true);
    expect(isPocketConnection({ ...base, name: "Remote", connection_settings: { base_url: "https://ai.example.com/voice/v1" } })).toBe(true);
    expect(isPocketConnection({ ...base, name: "Remote", connection_settings: { base_url: "https://ai.example.com/tts/v1" } })).toBe(true);
  });
});

describe("CloneVoiceDialog", () => {
  it("shows every problem and sends nothing until the form is complete", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    renderWithProviders(<CloneVoiceDialog open onOpenChange={() => undefined} connectionId="c1" available />);
    await userEvent.click(screen.getByRole("button", { name: "Clone voice" }));
    expect(screen.getByText("Give the voice a name.")).toBeInTheDocument();
    expect(screen.getByText("Add a voice sample: upload a file or record one.")).toBeInTheDocument();
    expect(screen.getByText("Confirm that the speaker gave permission before cloning their voice.")).toBeInTheDocument();

    await userEvent.upload(screen.getByTestId("voice-sample-input"), new File(["x"], "virus.exe", { type: "application/octet-stream" }), { applyAccept: false });
    expect(await screen.findByText(/isn't a supported type/)).toBeInTheDocument();
    expect(toVoiceSample).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("checks the file as chosen, then uploads a WAV with the consent attestation and selects the new voice", async () => {
    const calls = route({
      [CHECK]: () => json(200, CLEAN),
      "POST /api/v1/ai/connections/c1/voices": () => json(201, { id: "custom-sam-0123456789abcdef", name: "Sam", cloned: true, sample_seconds: 6, quality: CLEAN }),
    });
    // jsdom's FormData is not readable back from a Request: record what is appended instead.
    const append = vi.spyOn(FormData.prototype, "append");
    const onCreated = vi.fn();
    renderWithProviders(<CloneVoiceDialog open onOpenChange={() => undefined} connectionId="c1" available onCreated={onCreated} />);
    await userEvent.type(screen.getByPlaceholderText("Voice name"), "Sam");
    await userEvent.upload(screen.getByTestId("voice-sample-input"), new File(["mp3"], "sam.mp3", { type: "audio/mpeg" }));
    expect(await screen.findByText("sam.mp3")).toBeInTheDocument();
    // The original file is checked (its rate, channels and format matter), not the WAV.
    expect(await screen.findByText("Nothing to report: a clean recording.")).toBeInTheDocument();
    expect(screen.getByText("100/100")).toBeInTheDocument();
    expect(calls.map((r) => `${r.method} ${new URL(r.url).pathname}`)).toEqual([CHECK]);
    const [field, checked, filename] = append.mock.calls[0]!;
    expect([field, (checked as File).type, filename]).toEqual(["audio", "audio/mpeg", "sam.mp3"]);
    await userEvent.click(screen.getByRole("button", { name: "Clone voice" }));
    expect(screen.getByText(/Confirm that the speaker gave permission/)).toBeInTheDocument();
    expect(calls).toHaveLength(1);

    await userEvent.click(screen.getByRole("checkbox", { name: "I have the speaker's permission to clone their voice." }));
    await userEvent.click(screen.getByRole("button", { name: "Clone voice" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ id: "custom-sam-0123456789abcdef" })));
    const req = calls[1]!;
    expect(req.method).toBe("POST");
    expect(new URL(req.url).pathname).toBe("/api/v1/ai/connections/c1/voices");
    const fields = Object.fromEntries(append.mock.calls.slice(1).map(([k, v]) => [k, v]));
    expect(fields.name).toBe("Sam");
    expect(fields.consent_attestation).toBe("true");
    expect((fields.audio as Blob).type).toBe("audio/wav");
    append.mockRestore();
  });

  it("blocks cloning on a problem finding and shows the warnings and notes", async () => {
    let reply = CLIPPED;
    const calls = route({
      [CHECK]: () => json(200, reply),
      "POST /api/v1/ai/connections/c1/voices": () => json(201, { id: "custom-sam-0123456789abcdef", name: "Sam", cloned: true }),
    });
    renderWithProviders(<CloneVoiceDialog open onOpenChange={() => undefined} connectionId="c1" available />);
    await userEvent.type(screen.getByPlaceholderText("Voice name"), "Sam");
    await userEvent.click(screen.getByRole("checkbox", { name: "I have the speaker's permission to clone their voice." }));
    await userEvent.upload(screen.getByTestId("voice-sample-input"), new File(["wav"], "loud.wav", { type: "audio/wav" }));
    const report = await screen.findByRole("group", { name: "Recording quality" });
    expect(within(report).getByText("41/100")).toBeInTheDocument();
    expect(within(report).getByText(/The recording clips in 2.1 % of samples/)).toBeInTheDocument();
    expect(within(report).getByText(/Short \(5.9 s\)/)).toBeInTheDocument();
    expect(within(report).getByText("Stereo: the two channels were mixed to one.")).toBeInTheDocument();
    expect(within(report).getAllByText(/^(Problem|Warning|Note):$/).map((e) => e.textContent)).toEqual(["Problem:", "Warning:", "Note:"]);
    expect(within(report).getByText("Fix the problems above and add the recording again.")).toBeInTheDocument();
    const clone = screen.getByRole("button", { name: "Clone voice" });
    expect(clone).toBeDisabled();
    await userEvent.click(clone);
    expect(calls.filter((r) => new URL(r.url).pathname.endsWith("/voices"))).toHaveLength(0);

    // Warnings alone do not block.
    reply = { ...CLIPPED, score: 88, findings: CLIPPED.findings.slice(1) };
    await userEvent.upload(screen.getByTestId("voice-sample-input"), new File(["wav"], "better.wav", { type: "audio/wav" }));
    expect(await screen.findByText("88/100")).toBeInTheDocument();
    expect(screen.queryByText(/The recording clips/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clone voice" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Clone voice" }));
    await waitFor(() => expect(calls.filter((r) => new URL(r.url).pathname.endsWith("/voices"))).toHaveLength(1));
  });

  it("lets the clone go ahead when the check itself fails, and shows a refused clone's findings", async () => {
    route({
      [CHECK]: () => json(503, { error: { code: "integration_unavailable", message: "The voice server did not answer in time.", details: null } }),
      "POST /api/v1/ai/connections/c1/voices": () =>
        json(422, {
          error: {
            code: "validation_failed",
            message: "Background noise is almost as loud as the voice (estimated 4 dB below it): record in a quieter place, closer to the microphone.",
            details: { field: "audio", score: 53, findings: [{ level: "problem", code: "noise", message: "Background noise is almost as loud as the voice (estimated 4 dB below it): record in a quieter place, closer to the microphone." }, { level: "warning", code: "short", message: "Short (6 s): 10 to 20 seconds of speech gives a better clone." }] },
          },
        }),
    });
    renderWithProviders(<CloneVoiceDialog open onOpenChange={() => undefined} connectionId="c1" available />);
    await userEvent.type(screen.getByPlaceholderText("Voice name"), "Sam");
    await userEvent.click(screen.getByRole("checkbox", { name: "I have the speaker's permission to clone their voice." }));
    await userEvent.upload(screen.getByTestId("voice-sample-input"), new File(["wav"], "sam.wav", { type: "audio/wav" }));
    expect(await screen.findByText(/couldn't be checked right now/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clone voice" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Clone voice" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Background noise is almost as loud/);
    const report = screen.getByRole("group", { name: "Recording quality" });
    expect(within(report).getByText(/Short \(6 s\)/)).toBeInTheDocument();
    expect(within(report).queryByText(/\/100/)).not.toBeInTheDocument();
  });

  it("treats a check the API refuses outright as a bad sample", async () => {
    route({ [CHECK]: () => json(415, { error: { code: "unsupported_media_type", message: "Could not read this audio. Upload a WAV, MP3, M4A (AAC), OGG (Opus or Vorbis), WebM (Opus) or FLAC file.", details: null } }) });
    renderWithProviders(<CloneVoiceDialog open onOpenChange={() => undefined} connectionId="c1" available />);
    await userEvent.upload(screen.getByTestId("voice-sample-input"), new File(["wav"], "odd.wav", { type: "audio/wav" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not read this audio/);
    expect(screen.queryByText("odd.wav")).not.toBeInTheDocument();
  });

  it("goes back to turning voice cloning on when the server answers 501", async () => {
    route({
      [CHECK]: () => json(200, CLEAN),
      "POST /api/v1/ai/connections/c1/voices": () => json(501, { error: { code: "voice_cloning_unavailable", message: SETUP, details: { setup_url: TERMS } } }),
      "GET /api/v1/ai/connections/c1/voice-cloning": () => json(200, status("off")),
    });
    renderWithProviders(<CloneVoiceDialog open onOpenChange={() => undefined} connectionId="c1" available />);
    await userEvent.type(screen.getByPlaceholderText("Voice name"), "Sam");
    await userEvent.upload(screen.getByTestId("voice-sample-input"), new File(["wav"], "sam.wav", { type: "audio/wav" }));
    await screen.findByText("sam.wav");
    await userEvent.click(screen.getByRole("checkbox", { name: "I have the speaker's permission to clone their voice." }));
    await userEvent.click(screen.getByRole("button", { name: "Clone voice" }));
    expect(await screen.findByRole("heading", { name: "Turn on voice cloning" })).toBeInTheDocument();
    expect(await screen.findByLabelText(/Hugging Face access token/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /kyutai\/pocket-tts/ })).toHaveAttribute("href", TERMS);
  });
});

describe("Turning voice cloning on", () => {
  afterEach(() => signInAs("admin"));

  it("lets an owner turn it on with a token, then shows the clone form", async () => {
    signInAs("owner");
    let state = "off";
    const calls = route({
      "GET /api/v1/ai/connections/c1/voice-cloning": () => json(200, status(state, { token_source: state === "off" ? null : "saved" })),
      "PUT /api/v1/ai/connections/c1/voice-cloning": () => {
        state = "enabling";
        return json(202, status("enabling", { token_source: "saved" }));
      },
      "GET /api/v1/ai/connections/c1/voices": () => json(200, { voices: [], cloning_available: state === "on", cloning_state: state }),
    });
    renderWithProviders(<CloneVoiceDialog open onOpenChange={() => undefined} connectionId="c1" available={false} />);
    const token = await screen.findByLabelText(/Hugging Face access token/);

    // Nothing is sent until the token looks right and the terms are confirmed.
    await userEvent.type(token, "not a token");
    await userEvent.click(screen.getByRole("button", { name: /Turn on voice cloning/ }));
    expect(screen.getByText("A Hugging Face token starts with hf_ and has no spaces.")).toBeInTheDocument();
    expect(screen.getByText("Confirm that the token's account accepted the terms.")).toBeInTheDocument();
    expect(calls.filter((r) => r.method === "PUT")).toHaveLength(0);

    await userEvent.clear(token);
    await userEvent.type(token, "hf_TestOnlyNotARealToken0123456789ab");
    await userEvent.click(screen.getByRole("checkbox", { name: /accepted the terms of kyutai\/pocket-tts/ }));
    await userEvent.click(screen.getByRole("button", { name: /Turn on voice cloning/ }));
    expect(await screen.findByText("Turning voice cloning on…")).toBeInTheDocument();
    const put = calls.find((r) => r.method === "PUT")!;
    expect(await put.clone().json()).toEqual({ hf_token: "hf_TestOnlyNotARealToken0123456789ab", terms_accepted: true });

    state = "on"; // the server finished downloading the weights
    expect(await screen.findByRole("button", { name: "Clone voice" }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Clone a voice" })).toBeInTheDocument();
  });

  it("shows why the voice server refused the token", async () => {
    signInAs("owner");
    route({
      "GET /api/v1/ai/connections/c1/voice-cloning": () => json(200, status("off")),
      "PUT /api/v1/ai/connections/c1/voice-cloning": () =>
        json(422, { error: { code: "validation_failed", message: "Hugging Face says this token's account has not accepted the terms.", details: { field: "hf_token", terms_url: TERMS } } }),
    });
    renderWithProviders(<CloneVoiceDialog open onOpenChange={() => undefined} connectionId="c1" available={false} />);
    await userEvent.type(await screen.findByLabelText(/Hugging Face access token/), "hf_TestOnlyNotARealToken0123456789ab");
    await userEvent.click(screen.getByRole("checkbox", { name: /accepted the terms of kyutai\/pocket-tts/ }));
    await userEvent.click(screen.getByRole("button", { name: /Turn on voice cloning/ }));
    expect(await screen.findByText("Hugging Face says this token's account has not accepted the terms.")).toBeInTheDocument();
    expect(screen.getByLabelText(/Hugging Face access token/)).toHaveValue("hf_TestOnlyNotARealToken0123456789ab");
  });

  it("tells everyone else who can turn it on, and shows a failed attempt", async () => {
    route({ "GET /api/v1/ai/connections/c1/voice-cloning": () => json(200, status("failed", { can_manage: false, message: "The voice-cloning model could not be downloaded." })) });
    renderWithProviders(<CloneVoiceDialog open onOpenChange={() => undefined} connectionId="c1" available={false} />);
    expect(await screen.findByText("The voice-cloning model could not be downloaded.")).toBeInTheDocument();
    expect(screen.getByText(/turned on by an organization owner/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Hugging Face access token/)).not.toBeInTheDocument();
  });
});

describe("PocketVoicePicker", () => {
  afterEach(() => signInAs("admin"));

  it("lists cloned voices with a badge and leads to turning voice cloning on", async () => {
    route({
      "GET /api/v1/ai/connections/c1/voices": () =>
        json(200, {
          voices: [
            { id: "alba", name: "alba", cloned: false, license: "CC-BY-4.0", default: true },
            { id: "custom-sam-0123456789abcdef", name: "Sam", cloned: true, sample_seconds: 6 },
          ],
          cloning_available: false,
          cloning_state: "off",
          setup_message: SETUP,
          setup_url: TERMS,
        }),
      "GET /api/v1/ai/connections/c1/voice-cloning": () => json(200, status("off", { can_manage: false })),
    });
    renderWithProviders(<PocketVoicePicker label="voice" value="custom-sam-0123456789abcdef" onChange={() => undefined} connectionId="c1" />);
    expect(await screen.findByText("Selected: Sam")).toBeInTheDocument();
    expect(screen.getByText("Cloned")).toBeInTheDocument();
    expect(screen.getByText(/Voice cloning is off for this server/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Clone a voice/ }));
    expect(await screen.findByRole("heading", { name: "Turn on voice cloning" })).toBeInTheDocument();
    expect(await screen.findByText(/turned on by an organization owner/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clone voice" })).not.toBeInTheDocument();
  });

  it("marks the voices of an expressive server with the sounds they make", async () => {
    route({
      "GET /api/v1/ai/connections/c1/voices": () =>
        json(200, {
          voices: [
            { id: "emily", name: "Emily", cloned: false, default: true },
            { id: "custom-sam-0123456789abcdef", name: "Sam", cloned: true, sample_seconds: 6 },
          ],
          cloning_available: true,
          cloning_state: "on",
          sound_tags: ["laugh", "chuckle", "sigh", "gasp", "cough"],
        }),
    });
    renderWithProviders(<PocketVoicePicker label="voice" value="emily" onChange={() => undefined} connectionId="c1" />);
    expect(await screen.findByText("Selected: Emily")).toBeInTheDocument();
    const badge = screen.getByText("Laughs, sighs");
    expect(badge).toHaveAttribute("title", "Can make these sounds: laugh, chuckle, sigh, gasp, cough");
    await userEvent.click(screen.getByRole("combobox"));
    // Every voice of the server can: built-in and cloned alike.
    expect(await screen.findAllByText("Laughs, sighs")).toHaveLength(3);
  });

  it("marks no voice of a server without sounds", async () => {
    route({
      "GET /api/v1/ai/connections/c1/voices": () => json(200, { voices: [{ id: "alba", name: "alba", cloned: false, default: true }], cloning_available: true, cloning_state: "on", sound_tags: [] }),
    });
    renderWithProviders(<PocketVoicePicker label="voice" value="alba" onChange={() => undefined} connectionId="c1" />);
    expect(await screen.findByText("Selected: alba")).toBeInTheDocument();
    expect(screen.queryByText("Laughs, sighs")).not.toBeInTheDocument();
  });

  it("lets an owner turn voice cloning off after confirming", async () => {
    signInAs("owner");
    const calls = route({
      "GET /api/v1/ai/connections/c1/voices": () => json(200, { voices: [{ id: "alba", name: "alba", cloned: false, default: true }], cloning_available: true, cloning_state: "on" }),
      "DELETE /api/v1/ai/connections/c1/voice-cloning": () => new Response(null, { status: 204 }),
    });
    renderWithProviders(<PocketVoicePicker label="voice" value="alba" onChange={() => undefined} connectionId="c1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Turn off" }));
    expect(screen.getByText("Turn voice cloning off?")).toBeInTheDocument();
    expect(calls.filter((r) => r.method === "DELETE")).toHaveLength(0);
    await userEvent.click(screen.getAllByRole("button", { name: "Turn off" }).at(-1)!);
    await waitFor(() => expect(calls.filter((r) => r.method === "DELETE")).toHaveLength(1));
  });
});
