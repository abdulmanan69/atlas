import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import KnowledgePage from "@/features/knowledge/knowledge-page";
import { formatBytes, validateFile } from "@/features/knowledge/queries";
import { UploadDialog } from "@/features/knowledge/upload-dialog";
import { session } from "@/lib/session";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

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
    memberships: [{ id: "m1", role: "manager", status: "active", organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "GB", created_at: "2026-01-01T00:00:00Z" } }],
  } as never);
});
afterEach(() => vi.unstubAllGlobals());

describe("file checks", () => {
  it("accepts the documented types within 5 MB and refuses the rest", () => {
    expect(validateFile(new File(["hi"], "prices.pdf"))).toBeNull();
    expect(validateFile(new File(["hi"], "faq.MD"))).toBeNull();
    expect(validateFile(new File(["hi"], "setup.exe"))).toMatch(/isn't a supported type/);
    expect(validateFile(new File([], "empty.txt"))).toMatch(/is empty/);
    const big = new File([new Uint8Array(5 * 1024 * 1024 + 1)], "big.pdf");
    expect(validateFile(big)).toMatch(/limit is 5 MB/);
    expect(formatBytes(5 * 1024 * 1024 + 1)).toBe("5.0 MB");
  });
});

describe("UploadDialog", () => {
  it("rejects a wrong file type client-side without calling the API", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    renderWithProviders(<UploadDialog open onOpenChange={() => undefined} />);
    await userEvent.upload(screen.getByTestId("knowledge-file-input"), new File(["x"], "virus.exe", { type: "application/octet-stream" }), { applyAccept: false });
    expect(await screen.findByRole("alert")).toHaveTextContent("isn't a supported type");
    expect(screen.getByRole("button", { name: "Upload" })).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("points at AI providers when no embeddings model is configured", async () => {
    const fetchMock = vi.fn(async (_req: Request) => json(422, { error: { code: "validation_failed", message: "Choose an embeddings model under AI providers before using the knowledge base." } }));
    vi.stubGlobal("fetch", fetchMock);
    renderWithProviders(<UploadDialog open onOpenChange={() => undefined} />);
    await userEvent.upload(screen.getByTestId("knowledge-file-input"), new File(["hello"], "faq.txt", { type: "text/plain" }));
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Choose an embeddings model under AI providers");
    expect(screen.getByRole("link", { name: "Choose one under AI providers" })).toHaveAttribute("href", "/ai-providers");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const req = fetchMock.mock.calls[0]![0] as Request;
    expect(req.method).toBe("POST");
    expect(new URL(req.url).pathname).toBe("/api/v1/knowledge/documents");
  });
});

describe("KnowledgePage", () => {
  it("renders the switched-off state when the API answers 404", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(404, { error: { code: "not_found", message: "The knowledge base is not enabled on this deployment." } })),
    );
    renderWithProviders(<KnowledgePage />);
    expect(await screen.findByText("Knowledge is switched off on this deployment")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Upload document/ })).not.toBeInTheDocument();
  });

  it("lists documents with their status and the processing disclosure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json(200, [
          { id: "d1", filename: "prices.pdf", status: "completed", ready: true, chunk_count: 12, size_bytes: 20480, created_at: "2026-10-17T09:00:00Z", error: null, mime_type: "application/pdf" },
          { id: "d2", filename: "old.docx", status: "failed", ready: false, chunk_count: 0, size_bytes: 1024, created_at: "2026-10-17T09:00:00Z", error: "Identical content already indexed", mime_type: "" },
        ]),
      ),
    );
    renderWithProviders(<KnowledgePage />);
    expect(await screen.findByText("prices.pdf")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Identical content already indexed")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("external document-processing service");
    expect(screen.getByLabelText("Search query")).toBeEnabled();
  });
});
