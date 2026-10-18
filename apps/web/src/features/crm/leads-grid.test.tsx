import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";

import { LeadsGrid } from "@/features/crm/leads-grid";
import { apiSortToColumn, blockToQuery, createLeadsDatasource, sortModelToApi, type LeadPage } from "@/features/crm/leads-grid-model";
import type { Lead, LeadQuery } from "@/features/queries";

const stages = [
  { id: "s-new", key: "new", name: "New", position: 0, category: "open" as const, is_default: true, color: null },
  { id: "s-won", key: "won", name: "Won", position: 1, category: "won" as const, is_default: false, color: null },
];

function lead(n: number, patch: Partial<Lead> = {}): Lead {
  return {
    id: `lead-${n}`,
    first_name: ["Ayesha", "Bilal", "Chen"][n % 3] ?? "Lead",
    last_name: `Number ${n}`,
    company: `Company ${n}`,
    email: null,
    phone_e164: "+442079460000",
    phone_display: "+44 20 7946 0000",
    do_not_call: n % 2 === 1,
    consent_status: "unknown",
    qualification_status: "unknown",
    custom_fields: {},
    job_title: null,
    last_contacted_at: null,
    next_follow_up_at: null,
    owner_user_id: n === 0 ? "u1" : null,
    source: "web_form",
    stage_id: "s-new",
    tags: [],
    timezone: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...patch,
  };
}

describe("sort mapping", () => {
  it("maps the grid sort model to the API enum", () => {
    expect(sortModelToApi([])).toBe("-created_at");
    expect(sortModelToApi([], "-updated_at")).toBe("-updated_at");
    expect(sortModelToApi([{ colId: "lead", sort: "asc" }])).toBe("first_name");
    expect(sortModelToApi([{ colId: "next_follow_up_at", sort: "asc" }])).toBe("next_follow_up_at");
    expect(sortModelToApi([{ colId: "created_at", sort: "asc" }])).toBe("created_at");
    expect(sortModelToApi([{ colId: "created_at", sort: "desc" }])).toBe("-created_at");
    expect(sortModelToApi([{ colId: "phone", sort: "asc" }])).toBe("-created_at");
  });

  it("maps API sorts back to a column, or null when no column shows it", () => {
    expect(apiSortToColumn("first_name")).toEqual({ colId: "lead", sort: "asc" });
    expect(apiSortToColumn("-created_at")).toEqual({ colId: "created_at", sort: "desc" });
    expect(apiSortToColumn("-updated_at")).toBeNull();
  });
});

describe("blockToQuery", () => {
  it("turns an infinite-model block into a page request", () => {
    expect(blockToQuery({ startRow: 0, endRow: 50 }, { q: "ay" }, "-created_at")).toEqual({ q: "ay", sort: "-created_at", page: 1, page_size: 50 });
    expect(blockToQuery({ startRow: 100, endRow: 150 }, {}, "first_name")).toMatchObject({ page: 3, page_size: 50 });
  });
});

describe("createLeadsDatasource", () => {
  const block = (sortModel: { colId: string; sort: "asc" | "desc" }[] = []) => ({
    startRow: 0,
    endRow: 50,
    sortModel,
    filterModel: {},
    successCallback: vi.fn(),
    failCallback: vi.fn(),
    context: undefined,
    api: undefined as never,
  });

  it("fetches the block with current filters and reports the total", async () => {
    const fetchPage = vi.fn(async (): Promise<LeadPage> => ({ items: [lead(0)], total: 7 }));
    const onResult = vi.fn();
    const ds = createLeadsDatasource({ fetchPage, getFilters: () => ({ stage_id: "s-new" }), getSort: () => "-created_at", onResult });
    const params = block([{ colId: "lead", sort: "asc" }]);
    ds.getRows(params);
    await waitFor(() => expect(params.successCallback).toHaveBeenCalledWith([lead(0)], 7));
    expect(fetchPage).toHaveBeenCalledWith({ stage_id: "s-new", sort: "first_name", page: 1, page_size: 50 });
    expect(onResult).toHaveBeenCalledWith({ items: [lead(0)], total: 7 });
  });

  it("fails the block and surfaces the error", async () => {
    const error = new Error("boom");
    const onError = vi.fn();
    const ds = createLeadsDatasource({ fetchPage: () => Promise.reject(error), getFilters: () => ({}), getSort: () => "-created_at", onError });
    const params = block();
    ds.getRows(params);
    await waitFor(() => expect(params.failCallback).toHaveBeenCalled());
    expect(onError).toHaveBeenCalledWith(error);
  });
});

function renderGrid(fetchPage: (q: LeadQuery) => Promise<LeadPage>, onSelectionChange = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/leads"]}>
        <Routes>
          <Route
            path="/leads"
            element={
              <LeadsGrid
                domLayout="autoHeight"
                fetchPage={fetchPage}
                filters={{}}
                sort="-created_at"
                onSortChange={() => undefined}
                selectable
                onSelectionChange={onSelectionChange}
                stages={stages}
                names={new Map([["u1", "Ayesha Khan"]])}
                emptyState={<p>Nothing here</p>}
              />
            }
          />
          <Route path="/leads/:leadId" element={<p>Lead detail</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LeadsGrid (jsdom)", () => {
  afterEach(() => {
    try {
      window.localStorage.removeItem("atlas-leads-grid");
    } catch {
      /* storage unavailable in this runtime */
    }
  });

  it("loads the first block through the datasource and renders rows", async () => {
    const fetchPage = vi.fn(async (): Promise<LeadPage> => ({ items: [lead(0), lead(1)], total: 2 }));
    renderGrid(fetchPage);
    expect(await screen.findByText("Ayesha Number 0", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(fetchPage).toHaveBeenCalledWith(expect.objectContaining({ page: 1, page_size: 50, sort: "-created_at" }));
    expect(screen.getByRole("status")).toHaveTextContent("2 leads");
    expect(screen.getByText("Ayesha Khan")).toBeInTheDocument();
    expect(screen.getByText("DNC")).toBeInTheDocument();
  });

  it("shows the empty state when nothing matches", async () => {
    renderGrid(async () => ({ items: [], total: 0 }));
    expect(await screen.findByText("Nothing here", {}, { timeout: 5000 })).toBeInTheDocument();
  });

  it("opens a lead when a row is clicked", async () => {
    renderGrid(async () => ({ items: [lead(0)], total: 1 }));
    await userEvent.click(await screen.findByText("Company 0", {}, { timeout: 5000 }));
    expect(await screen.findByText("Lead detail")).toBeInTheDocument();
  });
});
