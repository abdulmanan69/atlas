/**
 * Calls grid: AG Grid Community on the Infinite Row Model, the same pattern as the leads
 * grid. Rows stream from `GET /api/v1/calls` in blocks of 50 through react-query, so each
 * block is a cached query under ["org", orgId, "calls", …] and the grid refreshes itself
 * whenever that key is invalidated after a mutation. The list is always newest first.
 */
import { useQueryClient } from "@tanstack/react-query";
import {
  CellStyleModule,
  ColumnApiModule,
  GridStateModule,
  InfiniteRowModelModule,
  ModuleRegistry,
  ValidationModule,
  type CellKeyDownEvent,
  type ColDef,
  type FullWidthCellKeyDownEvent,
  type GetRowIdParams,
  type GridApi,
  type GridReadyEvent,
  type GridState,
  type RowClickedEvent,
  type StateUpdatedEvent,
} from "ag-grid-community";
import { AgGridReact, type CustomCellRendererProps } from "ag-grid-react";
import { ArrowDownLeft, ArrowUpRight, ChevronDown, Loader2, Mic } from "lucide-react";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router";

import { ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { MenuCheckboxItem, MenuContent, MenuItem, MenuLabel, MenuRoot, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { Badge, Skeleton } from "@/components/ui/primitives";
import {
  BLOCK_SIZE,
  createCallsDatasource,
  directionLabel,
  MODE_BADGE,
  OPTIONAL_COLUMNS,
  outcomeLabel,
  outcomeTone,
  STATE_BADGE,
  type CallFilters,
  type CallPage,
} from "@/features/calls/calls-model";
import { leadsGridTheme } from "@/features/crm/leads-grid-model";
import { qk, useOrgId, type Call, type CallQuery } from "@/features/queries";
import { api, unwrap } from "@/lib/api";
import { formatDateTime, formatDuration, formatNumber, formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

ModuleRegistry.registerModules([
  InfiniteRowModelModule,
  ColumnApiModule,
  GridStateModule,
  CellStyleModule,
  ...(import.meta.env.DEV ? [ValidationModule] : []),
]);

/* Renderers */

type CellProps = CustomCellRendererProps<Call>;

function WhenCell({ data }: CellProps) {
  if (!data) return null;
  const at = data.started_at ?? data.created_at;
  return (
    <div className="flex w-full min-w-0 flex-col justify-center leading-tight">
      <span className="truncate text-[13px] font-medium text-ink">{formatDateTime(at)}</span>
      <span className="truncate text-[12px] text-muted">{formatRelative(at)}</span>
    </div>
  );
}

function LeadCell({ data }: CellProps) {
  if (!data) return null;
  const test = data.mode === "test";
  const phone = data.direction === "inbound" ? data.from_phone_e164 : data.to_phone_e164;
  const name = data.lead_name ?? null;
  return (
    <div className="flex w-full min-w-0 flex-col justify-center leading-tight">
      {data.lead_id ? (
        <Link to={`/leads/${data.lead_id}`} tabIndex={-1} className="truncate font-semibold text-ink hover:text-accent">
          {name ?? phone ?? "Lead"}
        </Link>
      ) : (
        <span className="truncate font-semibold text-muted">{name ?? (test ? "Browser test call" : "Unknown caller")}</span>
      )}
      {test ? (
        <span className="truncate text-[12px] text-muted">{data.lead_id ? "Variables only, not contacted" : "Sample variables"}</span>
      ) : (
        <span className="truncate font-mono text-[12px] text-muted">{phone ?? "—"}</span>
      )}
    </div>
  );
}

function AgentCell({ data, names }: CellProps & { names?: Map<string, string> }) {
  if (!data) return null;
  if (!data.agent_id) return <span className="text-[12.5px] text-faint">—</span>;
  const name = names?.get(data.agent_id);
  return (
    <span className="inline-flex min-w-0 items-baseline gap-1.5 text-[12.5px]">
      {name ? <span className="truncate text-ink-2">{name}</span> : <Skeleton className="h-3 w-20" />}
      {data.agent_version ? <span className="shrink-0 font-mono text-[11px] text-faint">v{data.agent_version}</span> : null}
    </span>
  );
}

function DirectionCell({ data }: CellProps) {
  if (!data) return null;
  const Icon = data.mode === "test" ? Mic : data.direction === "inbound" ? ArrowDownLeft : ArrowUpRight;
  return (
    <span className="inline-flex items-center gap-1.5 text-[12.5px] text-ink-2">
      <Icon className="size-3.5 text-muted" aria-hidden />
      {directionLabel(data)}
    </span>
  );
}

function ModeCell({ data }: CellProps) {
  if (!data) return null;
  const m = MODE_BADGE[data.mode];
  return (
    <Badge tone={m.tone} title={m.description}>
      {m.label}
    </Badge>
  );
}

function StateCell({ data }: CellProps) {
  if (!data) return null;
  const s = STATE_BADGE[data.state];
  return (
    <span className="inline-flex items-center gap-1.5">
      <Badge tone={s.tone}>{s.label}</Badge>
      {data.opted_out ? (
        <Badge tone="danger" title="The person asked not to be contacted again">
          Opted out
        </Badge>
      ) : null}
    </span>
  );
}

function OutcomeCell({ data }: CellProps) {
  if (!data) return null;
  if (!data.outcome) return <span className="text-faint">—</span>;
  return <Badge tone={outcomeTone(data.outcome)}>{outcomeLabel(data.outcome)}</Badge>;
}

function DurationCell({ data }: CellProps) {
  if (!data) return null;
  return <span className={cn("font-mono text-[12.5px]", data.duration_seconds === null ? "text-faint" : "text-ink-2")}>{formatDuration(data.duration_seconds)}</span>;
}

function CapturedCell({ data }: CellProps) {
  if (!data) return null;
  const n = data.captured_count ?? 0;
  return <span className={cn("font-mono text-[12.5px]", n === 0 ? "text-faint" : "text-ink-2")}>{n === 0 ? "—" : formatNumber(n)}</span>;
}

function LoadingRow() {
  return (
    <div className="flex h-full w-full items-center gap-8 px-3" aria-hidden>
      <Skeleton className="h-3.5 w-32" />
      <Skeleton className="h-3.5 w-40" />
      <Skeleton className="h-3.5 w-24" />
      <Skeleton className="h-3.5 w-16" />
      <Skeleton className="h-3.5 w-20" />
    </div>
  );
}

/* Grid */

const STATE_KEY = "atlas-calls-grid";
const DEFAULT_COL: ColDef<Call> = { sortable: false, resizable: true, suppressHeaderMenuButton: true, minWidth: 80 };
const getRowId = ({ data }: GetRowIdParams<Call>) => data.id;

function loadState(): GridState | undefined {
  try {
    const raw = localStorage.getItem(STATE_KEY);
    return raw ? (JSON.parse(raw) as GridState) : undefined;
  } catch {
    return undefined;
  }
}
function saveState(state: GridState | null): void {
  try {
    if (!state) localStorage.removeItem(STATE_KEY);
    else localStorage.setItem(STATE_KEY, JSON.stringify({ columnVisibility: state.columnVisibility, columnOrder: state.columnOrder, columnSizing: state.columnSizing }));
  } catch {
    /* not persisted */
  }
}

export interface CallsGridHandle {
  refresh: () => void;
}

export interface CallsGridProps {
  filters: CallFilters;
  agentNames: Map<string, string>;
  /** Rendered over the grid when the query matches nothing. */
  emptyState: ReactNode;
  /** `normal` scrolls inside a fixed-height viewport (needed for block loading); `autoHeight` is for tests. */
  domLayout?: "normal" | "autoHeight";
  /** Override the fetcher (tests). Defaults to the calls API through react-query. */
  fetchPage?: (query: CallQuery) => Promise<CallPage>;
  className?: string;
}

export const CallsGrid = forwardRef<CallsGridHandle, CallsGridProps>(function CallsGrid(
  { filters, agentNames, emptyState, domLayout = "normal", fetchPage, className },
  ref,
) {
  const qc = useQueryClient();
  const orgId = useOrgId();
  const navigate = useNavigate();
  const apiRef = useRef<GridApi<Call> | null>(null);
  const filtersRef = useRef(filters);
  const pending = useRef(0);
  const [initialState] = useState(loadState);
  const [hidden, setHidden] = useState<Set<string>>(() => new Set(initialState?.columnVisibility?.hiddenColIds ?? []));
  const [status, setStatus] = useState<{ total: number | null; loading: boolean; error: unknown }>({ total: null, loading: true, error: null });
  filtersRef.current = filters;

  const defaultFetch = useCallback(
    (query: CallQuery) =>
      qc.fetchQuery({
        queryKey: qk.calls(orgId, query),
        queryFn: () => unwrap(api.GET("/api/v1/calls", { params: { query } })),
        staleTime: 15_000,
      }),
    [qc, orgId],
  );
  const fetchRef = useRef(fetchPage ?? defaultFetch);
  fetchRef.current = fetchPage ?? defaultFetch;

  const datasource = useMemo(
    () =>
      createCallsDatasource({
        fetchPage: (q) => fetchRef.current(q),
        getFilters: () => filtersRef.current,
        onStart: () => {
          pending.current += 1;
          setStatus((s) => ({ ...s, loading: true }));
        },
        onResult: (page) => {
          pending.current -= 1;
          setStatus({ total: page.total, loading: pending.current > 0, error: null });
        },
        onError: (error) => {
          pending.current -= 1;
          setStatus((s) => ({ ...s, loading: pending.current > 0, error }));
        },
      }),
    [],
  );

  // Filters changed: drop every block and start again from the top.
  const filtersKey = JSON.stringify(filters);
  const firstRun = useRef(true);
  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false;
      return;
    }
    apiRef.current?.purgeInfiniteCache();
  }, [filtersKey]);

  // Mutations invalidate ["org", orgId, "calls"]; refresh the loaded blocks in place.
  useEffect(() => {
    let scheduled = false;
    return qc.getQueryCache().subscribe((event) => {
      if (event.type !== "updated" || event.action.type !== "invalidate") return;
      const key = event.query.queryKey;
      if (key[0] !== "org" || key[2] !== "calls" || scheduled) return;
      scheduled = true;
      queueMicrotask(() => {
        scheduled = false;
        apiRef.current?.refreshInfiniteCache();
      });
    });
  }, [qc]);

  useImperativeHandle(ref, () => ({ refresh: () => apiRef.current?.refreshInfiniteCache() }), []);

  const columnDefs = useMemo<ColDef<Call>[]>(
    () => [
      { colId: "when", field: "created_at", headerName: "When", pinned: "left", lockPinned: true, lockVisible: true, width: 170, minWidth: 150, cellRenderer: WhenCell },
      { colId: "lead", field: "lead_id", headerName: "Lead", lockVisible: true, width: 220, minWidth: 160, cellRenderer: LeadCell },
      { colId: "agent", field: "agent_id", headerName: "Agent", width: 170, cellRenderer: AgentCell, cellRendererParams: { names: agentNames } },
      { colId: "direction", field: "direction", headerName: "Direction", width: 120, cellRenderer: DirectionCell },
      { colId: "mode", field: "mode", headerName: "Mode", width: 110, cellRenderer: ModeCell },
      { colId: "state", field: "state", headerName: "State", width: 150, cellRenderer: StateCell },
      { colId: "outcome", field: "outcome", headerName: "Outcome", width: 160, cellRenderer: OutcomeCell },
      { colId: "duration", field: "duration_seconds", headerName: "Duration", width: 110, cellClass: "justify-end", cellRenderer: DurationCell },
      { colId: "captured", field: "captured_count", headerName: "Captured", width: 110, flex: 1, cellRenderer: CapturedCell },
    ],
    [agentNames],
  );

  const onGridReady = useCallback((e: GridReadyEvent<Call>) => {
    apiRef.current = e.api;
  }, []);
  const onRowClicked = useCallback(
    (e: RowClickedEvent<Call>) => {
      if (!e.data) return;
      const target = e.event?.target as HTMLElement | null;
      if (target?.closest("a, button")) return;
      void navigate(`/calls/${e.data.id}`);
    },
    [navigate],
  );
  const onCellKeyDown = useCallback(
    (e: CellKeyDownEvent<Call> | FullWidthCellKeyDownEvent<Call>) => {
      const key = (e.event as KeyboardEvent | null)?.key;
      if (key !== "Enter" || !e.data) return;
      e.event?.preventDefault();
      void navigate(`/calls/${e.data.id}`);
    },
    [navigate],
  );
  const onStateUpdated = useCallback((e: StateUpdatedEvent<Call>) => {
    if (!e.sources.some((s) => s === "columnVisibility" || s === "columnOrder" || s === "columnSizing")) return;
    saveState(e.state);
    setHidden(new Set(e.state.columnVisibility?.hiddenColIds ?? []));
  }, []);

  const retry = () => {
    setStatus((s) => ({ ...s, error: null, loading: true }));
    apiRef.current?.purgeInfiniteCache();
  };
  const showEmpty = status.total === 0 && !status.loading && !status.error;

  return (
    <div className={cn("flex flex-col", className)}>
      <div className={cn("atlas-grid relative", domLayout === "normal" && "h-[max(420px,calc(100dvh-340px))]")} aria-busy={status.loading || undefined}>
        <AgGridReact<Call>
          theme={leadsGridTheme}
          className={domLayout === "normal" ? "h-full" : undefined}
          domLayout={domLayout}
          rowModelType="infinite"
          datasource={datasource}
          cacheBlockSize={BLOCK_SIZE}
          // 10 blocks = 500 rows: scrolling back up is served from memory, not refetched.
          maxBlocksInCache={10}
          cacheOverflowSize={1}
          maxConcurrentDatasourceRequests={2}
          infiniteInitialRowCount={8}
          blockLoadDebounceMillis={40}
          getRowId={getRowId}
          columnDefs={columnDefs}
          defaultColDef={DEFAULT_COL}
          rowHeight={44}
          headerHeight={36}
          loadingCellRenderer={LoadingRow}
          initialState={initialState}
          onGridReady={onGridReady}
          onRowClicked={onRowClicked}
          onCellKeyDown={onCellKeyDown}
          onStateUpdated={onStateUpdated}
          suppressDragLeaveHidesColumns
          suppressNoRowsOverlay
          enableCellTextSelection
          animateRows={false}
          suppressColumnVirtualisation={domLayout === "autoHeight"}
          suppressRowVirtualisation={domLayout === "autoHeight"}
        />
        {status.error ? (
          <div className="absolute inset-0 z-[1] bg-surface">
            <ErrorState error={status.error} onRetry={retry} />
          </div>
        ) : showEmpty ? (
          <div className="absolute inset-0 z-[1] bg-surface">{emptyState}</div>
        ) : null}
      </div>
      <div className="flex items-center justify-between gap-3 border-t border-line px-4 py-2 text-[12.5px] text-muted">
        <span role="status" className="inline-flex items-center gap-2">
          {status.total === null ? (
            <Skeleton className="h-3 w-16" />
          ) : (
            <>
              <span className="font-mono text-ink-2">{formatNumber(status.total)}</span> {status.total === 1 ? "call" : "calls"}
            </>
          )}
          {status.loading ? <Loader2 className="size-3.5 animate-spin" aria-label="Loading" /> : null}
        </span>
        <MenuRoot>
          <MenuTrigger asChild>
            <Button size="sm" variant="ghost">
              Columns <ChevronDown />
            </Button>
          </MenuTrigger>
          <MenuContent align="end">
            <MenuLabel>Show columns</MenuLabel>
            {OPTIONAL_COLUMNS.map((c) => (
              <MenuCheckboxItem key={c.colId} checked={!hidden.has(c.colId)} onCheckedChange={(v) => apiRef.current?.setColumnsVisible([c.colId], v)}>
                {c.label}
              </MenuCheckboxItem>
            ))}
            <MenuSeparator />
            <MenuItem
              onSelect={() => {
                apiRef.current?.resetColumnState();
                saveState(null);
                setHidden(new Set());
              }}
            >
              Reset layout
            </MenuItem>
          </MenuContent>
        </MenuRoot>
      </div>
    </div>
  );
});
