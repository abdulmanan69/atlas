import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router";

import { serviceSummary, type AiServerService } from "@/features/voice/ai-servers";
import { DesignVoiceDialog } from "@/features/voice/design-voice-dialog";
import { PocketVoicePicker } from "@/features/voice/pocket-voice-picker";
import { isCustomVoice, validateDesign, type PocketVoice, type VoiceCapabilities } from "@/features/voice/voice-cloning";
import { session } from "@/lib/session";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const WAV = new Uint8Array([82, 73, 70, 70, 0, 0, 0, 0, 87, 65, 86, 69]);
const DESCRIPTION = "A warm, unhurried woman in her forties with a soft Irish accent.";
const CAPABILITIES: VoiceCapabilities = {
  model: "qwen3-tts",
  cloning: true,
  design: true,
  instruct: true,
  sound_tags: [],
  languages: ["en", "de"],
  streaming: "sentence",
  reference_seconds: { min: 3, max: 30 },
};
const designed = (id: string, name: string): PocketVoice => ({ id, name, cloned: false, designed: true, description: DESCRIPTION, default: false });

function renderWithProviders(ui: React.ReactElement) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

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

/** Keeps the dialog open until it asks to close, as the picker does. */
function Host({ capabilities = CAPABILITIES, onCreated, onClose }: { capabilities?: VoiceCapabilities; onCreated?: (v: PocketVoice) => void; onClose?: () => void }) {
  const [open, setOpen] = useState(true);
  return open ? (
    <DesignVoiceDialog
      open
      onOpenChange={(o) => {
        if (!o) {
          setOpen(false);
          onClose?.();
        }
      }}
      connectionId="c1"
      capabilities={capabilities}
      onCreated={onCreated}
    />
  ) : (
    <p>closed</p>
  );
}

/** Pastes the description (typing 65 characters one by one is slow in jsdom). */
async function pasteDescription(text: string) {
  await userEvent.click(screen.getByPlaceholderText(/soft Irish accent/));
  await userEvent.paste(text);
}

beforeEach(() => {
  session.getState().signIn({
    access_token: "t",
    token_type: "bearer",
    expires_in: 900,
    user: { id: "u1", email: "a@example.com", full_name: "Ada", created_at: "2026-01-01T00:00:00Z" },
    memberships: [{ id: "m1", role: "admin", status: "active", organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "GB", created_at: "2026-01-01T00:00:00Z" } }],
  } as never);
  // jsdom has neither object URLs nor media playback.
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:preview"), revokeObjectURL: vi.fn() }));
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("design helpers", () => {
  it("checks the name and description like the API does", () => {
    expect(validateDesign({ name: " ", description: "Too short" })).toEqual({
      name: "Give the voice a name.",
      description: "Describe the voice in at least 10 characters: who is speaking and how.",
    });
    expect(validateDesign({ name: "x".repeat(61), description: DESCRIPTION }).name).toMatch(/at most 60/);
    expect(validateDesign({ name: "Maeve", description: "x".repeat(301) }).description).toMatch(/at most 300/);
    expect(validateDesign({ name: "Maeve", description: `  ${DESCRIPTION}\n\n` })).toEqual({});
  });

  it("tells the organization's own voices from built-in ones", () => {
    expect(isCustomVoice(designed("design-maeve-0123456789abcdef", "Maeve"))).toBe(true);
    expect(isCustomVoice({ id: "custom-sam-0123456789abcdef", name: "Sam", cloned: true, default: false })).toBe(true);
    expect(isCustomVoice({ id: "alba", name: "alba", cloned: false, default: true })).toBe(false);
  });

  it("names voice design and instructions in a server's summary", () => {
    const service = { service: "voice", title: "Expressive voice", state: "up", up: true, models: ["qwen3-tts"], capabilities: CAPABILITIES } as AiServerService;
    expect(serviceSummary(service)).toBe("qwen3-tts · designs voices · takes delivery instructions");
    expect(serviceSummary({ ...service, capabilities: { ...CAPABILITIES, design: false, instruct: false } })).toBe("qwen3-tts");
    expect(serviceSummary({ ...service, capabilities: null })).toBe("qwen3-tts");
  });
});

describe("DesignVoiceDialog", () => {
  it("shows every problem and sends nothing until the form is complete", async () => {
    const calls = route({});
    renderWithProviders(<Host />);
    expect(screen.getByPlaceholderText(/soft Irish accent/)).toHaveValue("");
    expect(screen.getByText("Language")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(screen.getByText("Give the voice a name.")).toBeInTheDocument();
    expect(screen.getByText(/Describe the voice in at least 10 characters/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Done" })).not.toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it("hides the language choice when the server lists none", () => {
    route({});
    renderWithProviders(<Host capabilities={{ ...CAPABILITIES, languages: [] }} />);
    expect(screen.queryByText("Language")).not.toBeInTheDocument();
  });

  it("previews the voice, plays its sample and keeps it with Done", async () => {
    const voice = designed("design-maeve-0123456789abcdef", "Maeve");
    const calls = route({
      "POST /api/v1/ai/connections/c1/voices/design": () => json(201, voice),
      "GET /api/v1/ai/connections/c1/voices/design-maeve-0123456789abcdef/sample": () => new Response(WAV, { status: 200, headers: { "Content-Type": "audio/wav" } }),
    });
    const onCreated = vi.fn();
    const onClose = vi.fn();
    renderWithProviders(<Host onCreated={onCreated} onClose={onClose} />);
    await userEvent.type(screen.getByPlaceholderText("Voice name"), "Maeve");
    await pasteDescription(`  ${DESCRIPTION}  `);
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));

    expect(await screen.findByText("Preview of “Maeve”")).toBeInTheDocument();
    const post = calls.find((r) => r.method === "POST")!;
    expect(await post.clone().json()).toEqual({ name: "Maeve", description: DESCRIPTION });
    const audio = screen.getByLabelText("Preview of Maeve");
    expect(audio).toHaveAttribute("src", "blob:preview");
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Preview again" })).toBeInTheDocument();
    expect(onCreated).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ id: voice.id, designed: true }));
    expect(onClose).toHaveBeenCalled();
    expect(await screen.findByText("closed")).toBeInTheDocument();
    expect(calls.filter((r) => r.method === "DELETE")).toHaveLength(0);
  });

  it("replaces an earlier preview, and discards it when the dialog is closed without Done", async () => {
    let n = 0;
    const calls = route({
      "POST /api/v1/ai/connections/c1/voices/design": () => json(201, designed(`design-maeve-${String(++n).padStart(16, "0")}`, "Maeve")),
      "GET /api/v1/ai/connections/c1/voices/design-maeve-0000000000000001/sample": () => new Response(WAV, { status: 200, headers: { "Content-Type": "audio/wav" } }),
      "GET /api/v1/ai/connections/c1/voices/design-maeve-0000000000000002/sample": () => new Response(WAV, { status: 200, headers: { "Content-Type": "audio/wav" } }),
      "DELETE /api/v1/ai/connections/c1/voices/design-maeve-0000000000000001": () => new Response(null, { status: 204 }),
      "DELETE /api/v1/ai/connections/c1/voices/design-maeve-0000000000000002": () => new Response(null, { status: 204 }),
    });
    renderWithProviders(<Host />);
    await userEvent.type(screen.getByPlaceholderText("Voice name"), "Maeve");
    await pasteDescription(DESCRIPTION);
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByText("Preview of “Maeve”");

    await userEvent.click(screen.getByRole("button", { name: "Preview again" }));
    await waitFor(() => expect(calls.filter((r) => r.method === "POST")).toHaveLength(2));
    const deleted = () => calls.filter((r) => r.method === "DELETE").map((r) => new URL(r.url).pathname.split("/").at(-1));
    await waitFor(() => expect(deleted()).toEqual(["design-maeve-0000000000000001"]));

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("closed")).toBeInTheDocument();
    await waitFor(() => expect(deleted()).toEqual(["design-maeve-0000000000000001", "design-maeve-0000000000000002"]));
  });

  it("shows what the server refused next to the field", async () => {
    route({
      "POST /api/v1/ai/connections/c1/voices/design": () => json(422, { error: { code: "validation_failed", message: "Describe a voice, not a real person.", details: { field: "description" } } }),
    });
    renderWithProviders(<Host />);
    await userEvent.type(screen.getByPlaceholderText("Voice name"), "Maeve");
    await pasteDescription(DESCRIPTION);
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("Describe a voice, not a real person.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Done" })).not.toBeInTheDocument();
  });
});

describe("PocketVoicePicker with a voice design server", () => {
  const voices = {
    voices: [{ id: "vivian", name: "Vivian", cloned: false, default: true }, designed("design-maeve-0123456789abcdef", "Maeve")],
    cloning_available: true,
    cloning_state: "on",
    sound_tags: [],
  };

  it("offers Design a voice when the server can, and badges designed voices", async () => {
    route({
      "GET /api/v1/ai/connections/c1/voices": () => json(200, voices),
      "GET /api/v1/ai/connections/c1/capabilities": () => json(200, CAPABILITIES),
    });
    renderWithProviders(<PocketVoicePicker label="voice" value="design-maeve-0123456789abcdef" onChange={() => undefined} connectionId="c1" />);
    expect(await screen.findByText("Selected: Maeve")).toBeInTheDocument();
    expect(screen.getByText("Designed")).toBeInTheDocument();
    expect(screen.queryByText("Cloned")).not.toBeInTheDocument();
    // Beside "Clone a voice", not instead of it.
    expect(screen.getByRole("button", { name: /Clone a voice/ })).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: /Design a voice/ }));
    expect(await screen.findByRole("heading", { name: "Design a voice" })).toBeInTheDocument();
  });

  it("keeps Design a voice away from a server that cannot design", async () => {
    route({
      "GET /api/v1/ai/connections/c1/voices": () => json(200, voices),
      "GET /api/v1/ai/connections/c1/capabilities": () => json(200, { ...CAPABILITIES, design: false }),
    });
    renderWithProviders(<PocketVoicePicker label="voice" value="vivian" onChange={() => undefined} connectionId="c1" />);
    expect(await screen.findByText("Selected: Vivian")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Clone a voice/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Design a voice/ })).not.toBeInTheDocument();
  });
});
