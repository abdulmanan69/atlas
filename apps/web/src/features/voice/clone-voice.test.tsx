import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
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

describe("clone form validation", () => {
  it("requires a name, a 5-30 s sample and the speaker's permission", () => {
    expect(validateClone({ name: " ", consent: false, sample: null })).toEqual({
      name: "Give the voice a name.",
      sample: "Add a voice sample: upload a file or record one.",
      consent: "Confirm that the speaker gave permission before cloning their voice.",
    });
    expect(validateClone({ name: "x".repeat(61), consent: true, sample: sample(6) }).name).toMatch(/at most 60/);
    expect(validateClone({ name: "Sam", consent: true, sample: sample(4.9) }).sample).toMatch(/at least 5 seconds/);
    expect(validateClone({ name: "Sam", consent: true, sample: sample(30.5) }).sample).toMatch(/at most 30 seconds/);
    expect(validateClone({ name: "Sam", consent: true, sample: sample(6) })).toEqual({});
  });

  it("accepts WAV, MP3 and M4A files up to 10 MB", () => {
    expect(validateSampleFile(new File(["x"], "sam.wav"))).toBeNull();
    expect(validateSampleFile(new File(["x"], "sam.MP3"))).toBeNull();
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

  it("uploads a WAV with the consent attestation and selects the new voice", async () => {
    const fetchMock = vi.fn(async (_req: Request) => json(201, { id: "custom-sam-0123456789abcdef", name: "Sam", cloned: true, sample_seconds: 6 }));
    vi.stubGlobal("fetch", fetchMock);
    // jsdom's FormData is not readable back from a Request: record what is appended instead.
    const append = vi.spyOn(FormData.prototype, "append");
    const onCreated = vi.fn();
    renderWithProviders(<CloneVoiceDialog open onOpenChange={() => undefined} connectionId="c1" available onCreated={onCreated} />);
    await userEvent.type(screen.getByPlaceholderText("Voice name"), "Sam");
    await userEvent.upload(screen.getByTestId("voice-sample-input"), new File(["mp3"], "sam.mp3", { type: "audio/mpeg" }));
    expect(await screen.findByText("sam.mp3")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clone voice" }));
    expect(screen.getByText(/Confirm that the speaker gave permission/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("checkbox", { name: "I have the speaker's permission to clone their voice." }));
    await userEvent.click(screen.getByRole("button", { name: "Clone voice" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ id: "custom-sam-0123456789abcdef" })));
    const req = fetchMock.mock.calls[0]![0];
    expect(req.method).toBe("POST");
    expect(new URL(req.url).pathname).toBe("/api/v1/ai/connections/c1/voices");
    const fields = Object.fromEntries(append.mock.calls.map(([k, v]) => [k, v]));
    expect(fields.name).toBe("Sam");
    expect(fields.consent_attestation).toBe("true");
    expect((fields.audio as Blob).type).toBe("audio/wav");
    append.mockRestore();
  });

  it("switches to the setup steps when the server answers 501", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(501, { error: { code: "voice_cloning_unavailable", message: SETUP, details: { setup_url: "https://huggingface.co/kyutai/pocket-tts" } } })));
    renderWithProviders(<CloneVoiceDialog open onOpenChange={() => undefined} connectionId="c1" available />);
    await userEvent.type(screen.getByPlaceholderText("Voice name"), "Sam");
    await userEvent.upload(screen.getByTestId("voice-sample-input"), new File(["wav"], "sam.wav", { type: "audio/wav" }));
    await screen.findByText("sam.wav");
    await userEvent.click(screen.getByRole("checkbox", { name: "I have the speaker's permission to clone their voice." }));
    await userEvent.click(screen.getByRole("button", { name: "Clone voice" }));
    expect(await screen.findByText("Set up voice cloning")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(SETUP);
    expect(screen.getByText(/POCKET_TTS_HF_TOKEN=hf_/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "kyutai/pocket-tts" })).toHaveAttribute("href", "https://huggingface.co/kyutai/pocket-tts");
  });
});

describe("PocketVoicePicker", () => {
  it("lists cloned voices with a badge and explains setup when cloning is off", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json(200, {
          voices: [
            { id: "alba", name: "alba", cloned: false, license: "CC-BY-4.0", default: true },
            { id: "custom-sam-0123456789abcdef", name: "Sam", cloned: true, sample_seconds: 6 },
          ],
          cloning_available: false,
          setup_message: SETUP,
          setup_url: "https://huggingface.co/kyutai/pocket-tts",
        }),
      ),
    );
    renderWithProviders(<PocketVoicePicker label="voice" value="custom-sam-0123456789abcdef" onChange={() => undefined} connectionId="c1" />);
    expect(await screen.findByText("Selected: Sam")).toBeInTheDocument();
    expect(screen.getByText("Cloned")).toBeInTheDocument();
    expect(screen.getByText(/Voice cloning isn't set up on this server/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Clone a voice/ }));
    expect(await screen.findByText("Set up voice cloning")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(SETUP);
    expect(screen.queryByRole("button", { name: "Clone voice" })).not.toBeInTheDocument();
  });
});
