import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { ChevronRight, ScrollText } from "lucide-react";
import { Fragment, useState } from "react";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Card, Input, Skeleton } from "@/components/ui/primitives";
import { Select, SelectItem } from "@/components/ui/select";
import { Pagination, Table, Td, Th, Tr } from "@/components/ui/table";
import { qk, useMemberNames, useOrgId } from "@/features/queries";
import { api, unwrap } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 50;
const RESOURCES = ["lead", "membership", "invitation", "organization", "user"];

export default function AuditPage() {
  const orgId = useOrgId();
  const names = useMemberNames();
  const [action, setAction] = useState("");
  const [resource, setResource] = useState("");
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<string | null>(null);
  const query = { action: action.trim() || undefined, resource_type: resource || undefined, page, page_size: PAGE_SIZE };
  const logs = useQuery({
    queryKey: qk.audit(orgId, query),
    queryFn: () => unwrap(api.GET("/api/v1/audit-logs", { params: { query } })),
    placeholderData: keepPreviousData,
  });

  return (
    <>
      <PageHeader title="Audit log" description="Every change to this organization: who made it, when, and from where. Records can't be edited." />
      <Card className="overflow-hidden">
        <div className="flex flex-wrap gap-2 border-b border-line px-4 py-3">
          <Input
            value={action}
            onChange={(e) => {
              setAction(e.target.value);
              setPage(1);
            }}
            placeholder="Action starts with, e.g. lead."
            aria-label="Filter by action"
            className="w-64 font-mono"
          />
          <Select
            aria-label="Filter by resource"
            value={resource}
            onValueChange={(v) => {
              setResource(v);
              setPage(1);
            }}
            allowEmpty
            emptyLabel="All resources"
            className="w-44"
          >
            {RESOURCES.map((r) => (
              <SelectItem key={r} value={r}>
                {r}
              </SelectItem>
            ))}
          </Select>
        </div>
        {logs.isError ? (
          <ErrorState error={logs.error} onRetry={() => void logs.refetch()} />
        ) : logs.data && logs.data.total === 0 ? (
          <EmptyState icon={<ScrollText />} title="No matching events" />
        ) : (
          <>
            <Table>
              <thead>
                <tr>
                  <Th className="w-8" />
                  <Th>Time</Th>
                  <Th>Actor</Th>
                  <Th>Action</Th>
                  <Th>Resource</Th>
                  <Th>IP address</Th>
                </tr>
              </thead>
              <tbody className={cn(logs.isPlaceholderData && "opacity-60")}>
                {logs.data
                  ? logs.data.items.map((e) => {
                      const expanded = open === e.id;
                      const hasChanges = Object.keys(e.changes).length > 0;
                      return (
                        <Fragment key={e.id}>
                          <Tr className={hasChanges ? "cursor-pointer" : undefined} onClick={() => hasChanges && setOpen(expanded ? null : e.id)}>
                            <Td>
                              {hasChanges ? (
                                <ChevronRight className={cn("size-3.5 text-faint transition-transform", expanded && "rotate-90")} aria-label={expanded ? "Hide details" : "Show details"} />
                              ) : null}
                            </Td>
                            <Td className="whitespace-nowrap text-muted">{formatDateTime(e.created_at)}</Td>
                            <Td>{e.actor_user_id ? (names.get(e.actor_user_id) ?? "Former member") : <span className="text-muted">System</span>}</Td>
                            <Td>
                              <span className="font-mono text-[12px]">{e.action}</span>
                            </Td>
                            <Td className="font-mono text-[12px] text-muted">
                              {e.resource_type ? `${e.resource_type}${e.resource_id ? `/${e.resource_id.slice(0, 8)}` : ""}` : "—"}
                            </Td>
                            <Td className="font-mono text-[12px] text-muted">{e.ip_address ?? "—"}</Td>
                          </Tr>
                          {expanded ? (
                            <tr>
                              <td colSpan={6} className="border-b border-line bg-surface-2/60 px-12 py-3">
                                <pre className="overflow-x-auto font-mono text-[12px] text-ink-2">{JSON.stringify(e.changes, null, 2)}</pre>
                                {e.request_id ? <p className="mt-2 font-mono text-[11px] text-faint">request {e.request_id}</p> : null}
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      );
                    })
                  : Array.from({ length: 8 }, (_, i) => (
                      <tr key={i}>
                        <Td colSpan={6}>
                          <Skeleton className="h-5" />
                        </Td>
                      </tr>
                    ))}
              </tbody>
            </Table>
            {logs.data ? <Pagination page={page} pageSize={PAGE_SIZE} total={logs.data.total} onPageChange={setPage} /> : null}
          </>
        )}
      </Card>
    </>
  );
}
