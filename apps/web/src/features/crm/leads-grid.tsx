/**
 * Leads grid: AG Grid Community on the Infinite Row Model. Rows stream from
 * `GET /api/v1/leads` in blocks of 50 through react-query, so every block is a
 * cached query under ["org", orgId, "leads", …] and the grid refreshes itself
 * whenever that key is invalidated after a mutation.
 *
 * Only the modules the grid actually uses are registered (no AllCommunityModule),
 * and the route is lazy, so AG Grid ships in its own chunk. Pure mapping lives in
 * leads-grid-model.ts.
 */
import { useQueryClient } from "@tanstack/react-query";
import {
  CellStyleModule,
  ColumnApiModule,
  EventApiModule,
  GridStateModule,
  InfiniteRowModelModule,
  ModuleRegistry,
  RowApiModule,
  RowSelectionModule,
  ValidationModule,
  type CellKeyDownEvent,
  type ColDef,
  type FullWidthCellKeyDownEvent,
  type GetRowIdParams,
  type GridApi,
  type GridReadyEvent,
  type GridState,
  type IRowNode,
  type RowClickedEvent,
  type RowSelectionOptions,
  type SelectionColumnDef,
  type SortModelItem,
  type StateUpdatedEvent,
} from "ag-grid-community";
import { AgGridReact, type CustomCellRendererProps, type CustomHeaderProps } from "ag-grid-react";
import { ChevronDown, Loader2 } from "lucide-react";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router";

import { ErrorState, Phone } from "@/components/common";
import { StageTrack } from "@/components/stage-track";
import { Button } from "@/components/ui/button";
import { MenuCheckboxItem, MenuContent, MenuItem, MenuLabel, MenuRoot, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { Avatar, Badge, Checkbox, Skeleton } from "@/components/ui/primitives";
import {
  apiSortToColumn,
  BLOCK_SIZE,
  createLeadsDatasource,
  leadsGridTheme,
  OPTIONAL_COLUMNS,
  sortModelToApi,
  type LeadFilters,
  type LeadPage,
  type LeadSort,
} from "@/features/crm/leads-grid-model";
import { qk, useOrgId, type Lead, type LeadQuery, type Stage } from "@/features/queries";
import { api, unwrap } from "@/lib/api";
import { formatNumber, formatRelative, fullName } from "@/lib/format";
import { cn } from "@/lib/utils";

// Sorting, column move/resize, pinning and keyboard navigation ship inside the v36 core module.
ModuleRegistry.registerModules([
  InfiniteRowModelModule, // server-side blocks
  RowSelectionModule, // checkbox selection
  RowApiModule, // forEachNode (header checkbox over loaded rows)
  EventApiModule, // addEventListener in the header checkbox
  ColumnApiModule, // setColumnsVisible / applyColumnState / resetColumnState
  GridStateModule, // initialState / onStateUpdated (column layout persistence)
  CellStyleModule, // cellClass
  ...(import.meta.env.DEV ? [ValidationModule] : []), // readable errors in dev/test only
]);

/* Renderers */

type CellProps = CustomCellRendererProps<Lead>;

function LeadCell({ data }: CellProps) {
  if (!data) return null;
  return (
    <div className="flex w-full min-w-0 flex-col justify-center leading-tight">
      <Link to={`/leads/${data.id}`} tabIndex={-1} className="truncate font-semibold text-ink hover:text-accent">
        {fullName(data.first_name, data.last_name)}
      </Link>
      <span className="truncate text-[12px] text-muted">{data.company ?? data.email ?? "—"}</span>
    </div>
  );
}

function PhoneCell({ data }: CellProps) {
  if (!data) return null;
  return (
    <span className="inline-flex items-center gap-1.5">
      <Phone value={data.phone_display} />
      {data.do_not_call ? (
        <Badge tone="danger" title="Do not call">
          DNC
        </Badge>
      ) : null}
    </span>
  );
}

function StageCell({ data, stages }: CellProps & { stages?: Stage[] }) {
  if (!data) return null;
  return stages ? <StageTrack stages={stages} currentStageId={data.stage_id} /> : <Skeleton className="h-3 w-24" />;
}

function OwnerCell({ data, names }: CellProps & { names?: Map<string, string> }) {
  if (!data) return null;
  if (!data.owner_user_id) return <span className="text-[12.5px] text-faint">Unassigned</span>;
  const name = names?.get(data.owner_user_id) ?? "Teammate";
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5 text-[12.5px]">
      <Avatar name={name} className="size-5 text-[9px]" />
      <span className="truncate">{name}</span>
    </span>
  );
}

function SourceCell({ data }: CellProps) {
  if (!data) return null;
  return <span className="font-mono text-[12px] text-muted">{data.source ?? "—"}</span>;
}

function FollowUpCell({ data }: CellProps) {
  if (!data) return null;
  const v = data.next_follow_up_at;
  if (!v) return <span className="text-faint">—</span>;
  const overdue = new Date(v).getTime() < Date.now();
  return <span className={cn("text-[12.5px]", overdue ? "font-medium text-danger" : "text-ink-2")}>{formatRelative(v)}</span>;
}

function AddedCell({ data }: CellProps) {
  if (!data) return null;
  return <span className="whitespace-nowrap text-[12.5px] text-muted">{formatRelative(data.created_at)}</span>;
}

/** Full-width placeholder for rows whose block is still loading. */
function LoadingRow() {
  return (
    <div className="flex h-full w-full items-center gap-8 px-3" aria-hidden>
      <Skeleton className="h-3.5 w-44" />
      <Skeleton className="h-3.5 w-28" />
      <Skeleton className="h-3.5 w-24" />
      <Skeleton className="h-3.5 w-20" />
    </div>
  );
}

/** Header checkbox that acts on loaded rows only: the Infinite Row Model never has them all. */
function SelectionHeader({ api }: CustomHeaderProps<Lead>) {
  const [state, setState] = useState({ all: false, some: false });
  useEffect(() => {
    const update = () => {
      let loaded = 0;
      let selected = 0;
      api.forEachNode((n) => {
        if (!n.data) return;
        loaded += 1;
        if (n.isSelected()) selected += 1;
      });
      setState({ all: loaded > 0 && selected === loaded, some: selected > 0 && selected < loaded });
    };
    update();
    api.addEventListener("selectionChanged", update);
    api.addEventListener("modelUpdated", update);
    return () => {
      if (api.isDestroyed()) return;
      api.removeEventListener("selectionChanged", update);
      api.removeEventListener("modelUpdated", update);
    };
  }, [api]);
  return (
    <Checkbox
      aria-label="Select all loaded leads"
      checked={state.all ? true : state.some ? "indeterminate" : false}
      onCheckedChange={(checked) => {
        const nodes: IRowNode<Lead>[] = [];
        api.forEachNode((n) => {
          if (n.data) nodes.push(n);
        });
        api.setNodesSelected({ nodes, newValue: checked });
      }}
    />
  );
}

/* Grid */

const SELECTION_COL_ID = "ag-Grid-SelectionColumn";
const STATE_KEY = "atlas-leads-grid";

const ROW_SELECTION: RowSelectionOptions<Lead> = {
  mode: "multiRow",
  checkboxes: true,
  headerCheckbox: false,
  enableClickSelection: false,
  enableSelectionWithoutKeys: false,
};
const SELECTION_COL: SelectionColumnDef = {
  headerComponent: SelectionHeader,
  width: 44,
  minWidth: 44,
  maxWidth: 44,
  pinned: "left",
  lockPinned: true,
  lockPosition: "left",
  resizable: false,
  suppressMovable: true,
  suppressHeaderMenuButton: true,
};
const DEFAULT_COL: ColDef<Lead> = { sortable: false, resizable: true, suppressHeaderMenuButton: true, minWidth: 90 };
const getRowId = ({ data }: GetRowIdParams<Lead>) => data.id;

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

export interface LeadsGridHandle {
  clearSelection: () => void;
  refresh: () => void;
}

export interface LeadsGridProps {
  filters: LeadFilters;
  sort: LeadSort;
  onSortChange: (sort: LeadSort) => void;
  selectable: boolean;
  onSelectionChange: (ids: string[]) => void;
  stages: Stage[] | undefined;
  names: Map<string, string>;
  /** Rendered over the grid when the query matches nothing. */
  emptyState: ReactNode;
  /** `normal` scrolls inside a fixed-height viewport (needed for block loading); `autoHeight` is for tests. */
  domLayout?: "normal" | "autoHeight";
  /** Override the fetcher (tests). Defaults to the leads API through react-query. */
  fetchPage?: (query: LeadQuery) => Promise<LeadPage>;
  className?: string;
}

export const LeadsGrid = forwardRef<LeadsGridHandle, LeadsGridProps>(function LeadsGrid(
  { filters, sort, onSortChange, selectable, onSelectionChange, stages, names, emptyState, domLayout = "normal", fetchPage, className },
  ref,
) {
  const qc = useQueryClient();
  const orgId = useOrgId();
  const navigate = useNavigate();
  const apiRef = useRef<GridApi<Lead> | null>(null);
  const filtersRef = useRef(filters);
  const sortRef = useRef(sort);
  const appliedSort = useRef(sort);
  const initialSort = useRef(apiSortToColumn(sort));
  const pending = useRef(0);
  const [initialState] = useState(loadState);
  const [hidden, setHidden] = useState<Set<string>>(() => new Set(initialState?.columnVisibility?.hiddenColIds ?? []));
  const [status, setStatus] = useState<{ total: number | null; loading: boolean; error: unknown }>({ total: null, loading: true, error: null });
  filtersRef.current = filters;
  sortRef.current = sort;

  const defaultFetch = useCallback(
    (query: LeadQuery) =>
      qc.fetchQuery({
        queryKey: qk.leads(orgId, query),
        queryFn: () => unwrap(api.GET("/api/v1/leads", { params: { query } })),
        staleTime: 30_000,
      }),
    [qc, orgId],
  );
  const fetchRef = useRef(fetchPage ?? defaultFetch);
  fetchRef.current = fetchPage ?? defaultFetch;

  // One datasource for the grid's lifetime; it reads filters/sort through refs.
  const datasource = useMemo(
    () =>
      createLeadsDatasource({
        fetchPage: (q) => fetchRef.current(q),
        getFilters: () => filtersRef.current,
        getSort: () => sortRef.current,
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
    const grid = apiRef.current;
    if (!grid) return;
    grid.deselectAll();
    onSelectionChange([]);
    grid.purgeInfiniteCache();
  }, [filtersKey]);

  // URL sort changed from outside (dashboard links): mirror it in the column state.
  useEffect(() => {
    const grid = apiRef.current;
    if (!grid || appliedSort.current === sort) return;
    appliedSort.current = sort;
    const want = apiSortToColumn(sort);
    const current = grid.getColumnState().find((c) => c.sort);
    if ((current?.colId ?? null) === (want?.colId ?? null) && (current?.sort ?? null) === (want?.sort ?? null)) {
      grid.purgeInfiniteCache();
      return;
    }
    grid.applyColumnState({ state: want ? [{ colId: want.colId, sort: want.sort }] : [], defaultState: { sort: null } });
  }, [sort]);

  // Mutations invalidate ["org", orgId, "leads"]; refresh the loaded blocks in place.
  useEffect(() => {
    let scheduled = false;
    return qc.getQueryCache().subscribe((event) => {
      if (event.type !== "updated" || event.action.type !== "invalidate") return;
      const key = event.query.queryKey;
      if (key[0] !== "org" || key[2] !== "leads" || scheduled) return;
      scheduled = true;
      queueMicrotask(() => {
        scheduled = false;
        apiRef.current?.refreshInfiniteCache();
      });
    });
  }, [qc]);

  useImperativeHandle(
    ref,
    () => ({
      clearSelection: () => apiRef.current?.deselectAll(),
      refresh: () => apiRef.current?.refreshInfiniteCache(),
    }),
    [],
  );

  const columnDefs = useMemo<ColDef<Lead>[]>(() => {
    const init = initialSort.current;
    const sortOf = (colId: string) => (init?.colId === colId ? init.sort : null);
    return [
      {
        colId: "lead",
        field: "first_name",
        headerName: "Lead",
        pinned: "left",
        lockPinned: true,
        lockVisible: true,
        width: 260,
        minWidth: 200,
        sortable: true,
        sortingOrder: ["asc", null],
        initialSort: sortOf("lead"),
        cellRenderer: LeadCell,
      },
      { colId: "phone", field: "phone_display", headerName: "Phone", width: 190, cellRenderer: PhoneCell },
      { colId: "stage", field: "stage_id", headerName: "Stage", width: 200, cellRenderer: StageCell, cellRendererParams: { stages } },
      { colId: "owner", field: "owner_user_id", headerName: "Owner", width: 170, cellRenderer: OwnerCell, cellRendererParams: { names } },
      { colId: "source", field: "source", headerName: "Source", width: 130, cellRenderer: SourceCell },
      {
        colId: "next_follow_up_at",
        field: "next_follow_up_at",
        headerName: "Follow-up",
        width: 140,
        sortable: true,
        sortingOrder: ["asc", null],
        initialSort: sortOf("next_follow_up_at"),
        cellRenderer: FollowUpCell,
      },
      {
        colId: "created_at",
        field: "created_at",
        headerName: "Added",
        width: 130,
        flex: 1,
        sortable: true,
        sortingOrder: ["desc", "asc", null],
        initialSort: sortOf("created_at"),
        cellRenderer: AddedCell,
      },
    ];
  }, [stages, names]);

  const onGridReady = useCallback((e: GridReadyEvent<Lead>) => {
    apiRef.current = e.api;
  }, []);
  const onSelectionChanged = useCallback(() => {
    const grid = apiRef.current;
    if (!grid) return;
    onSelectionChange(grid.getSelectedNodes().flatMap((n) => (n.data ? [n.data.id] : [])));
  }, [onSelectionChange]);
  const onSortChanged = useCallback(() => {
    const grid = apiRef.current;
    if (!grid) return;
    const model: SortModelItem[] = grid
      .getColumnState()
      .filter((c): c is typeof c & { sort: "asc" | "desc" } => c.sort === "asc" || c.sort === "desc")
      .map((c) => ({ colId: c.colId, sort: c.sort }));
    const next = sortModelToApi(model, model.length ? "-created_at" : sortRef.current);
    if (next === sortRef.current) return;
    appliedSort.current = next;
    onSortChange(next);
  }, [onSortChange]);
  const onRowClicked = useCallback(
    (e: RowClickedEvent<Lead>) => {
      if (!e.data) return;
      const target = e.event?.target as HTMLElement | null;
      if (target?.closest(`a, button, input, [role=checkbox], [col-id="${SELECTION_COL_ID}"]`)) return;
      void navigate(`/leads/${e.data.id}`);
    },
    [navigate],
  );
  const onCellKeyDown = useCallback(
    (e: CellKeyDownEvent<Lead> | FullWidthCellKeyDownEvent<Lead>) => {
      const key = (e.event as KeyboardEvent | null)?.key;
      if (key !== "Enter" || !e.data || !("column" in e) || e.column.getColId() === SELECTION_COL_ID) return;
      e.event?.preventDefault();
      void navigate(`/leads/${e.data.id}`);
    },
    [navigate],
  );
  const onStateUpdated = useCallback((e: StateUpdatedEvent<Lead>) => {
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
      <div
        className={cn("atlas-grid relative", domLayout === "normal" && "h-[max(420px,calc(100dvh-340px))]")}
        aria-busy={status.loading || undefined}
      >
        <AgGridReact<Lead>
          theme={leadsGridTheme}
          className={domLayout === "normal" ? "h-full" : undefined}
          domLayout={domLayout}
          rowModelType="infinite"
          datasource={datasource}
          cacheBlockSize={BLOCK_SIZE}
          maxBlocksInCache={2}
          cacheOverflowSize={1}
          maxConcurrentDatasourceRequests={2}
          infiniteInitialRowCount={8}
          blockLoadDebounceMillis={40}
          getRowId={getRowId}
          columnDefs={columnDefs}
          defaultColDef={DEFAULT_COL}
          rowHeight={44}
          headerHeight={36}
          rowSelection={selectable ? ROW_SELECTION : undefined}
          selectionColumnDef={SELECTION_COL}
          loadingCellRenderer={LoadingRow}
          initialState={initialState}
          onGridReady={onGridReady}
          onSelectionChanged={onSelectionChanged}
          onSortChanged={onSortChanged}
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
              <span className="font-mono text-ink-2">{formatNumber(status.total)}</span> {status.total === 1 ? "lead" : "leads"}
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
