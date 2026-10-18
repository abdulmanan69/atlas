/**
 * Pure configuration for the leads grid: the token-mapped theme, the mapping
 * between AG Grid's Infinite Row Model and `GET /api/v1/leads`, and the
 * datasource factory. Kept component-free so it is unit-testable and keeps
 * Fast Refresh working for the grid component.
 */
import { themeQuartz, type IDatasource, type IGetRowsParams, type SortModelItem } from "ag-grid-community";

import type { Lead, LeadQuery } from "@/features/queries";

/* Theme: every colour is a token, so light and dark need no extra work. */

const sans = "var(--font-sans, 'Public Sans Variable', ui-sans-serif, system-ui, sans-serif)";

export const leadsGridTheme = themeQuartz.withParams({
  accentColor: "var(--accent)",
  backgroundColor: "var(--surface)",
  foregroundColor: "var(--ink)",
  textColor: "var(--ink)",
  subtleTextColor: "var(--muted)",
  borderColor: "var(--line)",
  browserColorScheme: "inherit",
  fontFamily: sans,
  fontSize: 13,
  headerFontFamily: sans,
  headerFontSize: 11.5,
  headerFontWeight: 600,
  headerTextColor: "var(--muted)",
  headerBackgroundColor: "var(--surface)",
  headerCellHoverBackgroundColor: "var(--surface-2)",
  headerColumnResizeHandleColor: "var(--line-strong)",
  headerColumnBorder: false,
  headerRowBorder: { color: "var(--line)" },
  headerHeight: 36,
  rowHeight: 44,
  rowBorder: { color: "var(--line)" },
  columnBorder: false,
  pinnedColumnBorder: { color: "var(--line)" },
  spacing: 6,
  cellHorizontalPadding: 12,
  borderRadius: 6,
  wrapperBorder: false,
  wrapperBorderRadius: 0,
  rowHoverColor: "color-mix(in srgb, var(--surface-2) 70%, transparent)",
  selectedRowBackgroundColor: "color-mix(in srgb, var(--accent-soft) 60%, transparent)",
  rangeSelectionBorderColor: "var(--accent)",
  focusShadow: false,
  iconColor: "var(--muted)",
  iconSize: 14,
  checkboxBorderRadius: 4,
  checkboxBorderWidth: 1,
  checkboxUncheckedBackgroundColor: "var(--surface)",
  checkboxUncheckedBorderColor: "var(--line-strong)",
  checkboxCheckedBackgroundColor: "var(--accent)",
  checkboxCheckedBorderColor: "var(--accent)",
  checkboxCheckedShapeColor: "var(--accent-ink)",
  checkboxIndeterminateBackgroundColor: "var(--accent)",
  checkboxIndeterminateBorderColor: "var(--accent)",
  checkboxIndeterminateShapeColor: "var(--accent-ink)",
  rowLoadingSkeletonEffectColor: "var(--surface-2)",
  dragAndDropImageBackgroundColor: "var(--surface)",
  dragAndDropImageBorder: { color: "var(--line-strong)" },
  dragAndDropImageShadow: "var(--shadow-pop)",
});

/* Server mapping. */

export const BLOCK_SIZE = 50;
export type LeadSort = NonNullable<LeadQuery["sort"]>;
export type LeadFilters = Pick<LeadQuery, "q" | "stage_id" | "owner_user_id" | "unassigned" | "do_not_call" | "tag">;
export interface LeadPage {
  items: Lead[];
  total: number;
}

/** Grid sort model → the API's `sort` enum. Columns the API can't sort by fall back. */
export function sortModelToApi(model: SortModelItem[], fallback: LeadSort = "-created_at"): LeadSort {
  const first = model[0];
  if (!first) return fallback;
  switch (first.colId) {
    case "lead":
      return "first_name";
    case "next_follow_up_at":
      return "next_follow_up_at";
    case "created_at":
      return first.sort === "desc" ? "-created_at" : "created_at";
    default:
      return fallback;
  }
}

/** API sort → the column that displays it, or `null` for sorts with no column (e.g. `-updated_at`). */
export function apiSortToColumn(sort: LeadSort): { colId: string; sort: "asc" | "desc" } | null {
  switch (sort) {
    case "first_name":
      return { colId: "lead", sort: "asc" };
    case "next_follow_up_at":
      return { colId: "next_follow_up_at", sort: "asc" };
    case "created_at":
      return { colId: "created_at", sort: "asc" };
    case "-created_at":
      return { colId: "created_at", sort: "desc" };
    default:
      return null;
  }
}

/** One infinite-model block → one API page. */
export function blockToQuery(block: Pick<IGetRowsParams, "startRow" | "endRow">, filters: LeadFilters, sort: LeadSort): LeadQuery {
  const page_size = Math.max(1, block.endRow - block.startRow);
  return { ...filters, sort, page: Math.floor(block.startRow / page_size) + 1, page_size };
}

export function createLeadsDatasource(opts: {
  fetchPage: (query: LeadQuery) => Promise<LeadPage>;
  getFilters: () => LeadFilters;
  getSort: () => LeadSort;
  onStart?: () => void;
  onResult?: (page: LeadPage) => void;
  onError?: (error: unknown) => void;
}): IDatasource {
  return {
    getRows(params) {
      opts.onStart?.();
      const query = blockToQuery(params, opts.getFilters(), sortModelToApi(params.sortModel, opts.getSort()));
      opts.fetchPage(query).then(
        (page) => {
          params.successCallback(page.items, page.total);
          opts.onResult?.(page);
        },
        (error: unknown) => {
          params.failCallback();
          opts.onError?.(error);
        },
      );
    },
  };
}

/** Columns a user may hide. The Lead column is always visible. */
export const OPTIONAL_COLUMNS: { colId: string; label: string }[] = [
  { colId: "phone", label: "Phone" },
  { colId: "stage", label: "Stage" },
  { colId: "owner", label: "Owner" },
  { colId: "source", label: "Source" },
  { colId: "next_follow_up_at", label: "Follow-up" },
  { colId: "created_at", label: "Added" },
];
