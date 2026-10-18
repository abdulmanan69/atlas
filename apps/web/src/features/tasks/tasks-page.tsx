import { useMutation } from "@tanstack/react-query";
import { CheckSquare, Plus } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { toast } from "sonner";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Avatar, Badge, Card, Checkbox, Skeleton } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { Pagination } from "@/components/ui/table";
import { TaskDialog } from "@/features/tasks/task-dialog";
import { useInvalidate, useMemberNames, useTasks, type TaskQuery } from "@/features/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import { useHasRole } from "@/lib/session";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 50;

export default function TasksPage() {
  const [params, setParams] = useSearchParams();
  const scope = (params.get("scope") ?? "mine") as "mine" | "all";
  const status = (params.get("status") ?? "open") as "open" | "done";
  const page = Number(params.get("page") ?? 1) || 1;
  const [creating, setCreating] = useState(false);
  const canWrite = useHasRole("agent");
  const names = useMemberNames();
  const invalidate = useInvalidate();

  const query: TaskQuery = { mine: scope === "mine", status, page, page_size: PAGE_SIZE };
  const tasks = useTasks(query);
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    next.set(k, v);
    if (k !== "page") next.delete("page");
    setParams(next, { replace: true });
  };

  const toggle = useMutation({
    mutationFn: ({ id, done }: { id: string; done: boolean }) =>
      unwrap(api.PATCH("/api/v1/tasks/{task_id}", { params: { path: { task_id: id } }, body: { status: done ? "done" : "open" } })),
    onSuccess: (t) => {
      toast.success(t.status === "done" ? "Task completed" : "Task reopened");
      void invalidate();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const now = Date.now();
  return (
    <>
      <PageHeader
        title="Tasks"
        description="Follow-ups for your team. Completing a task logs it on the lead's timeline."
        actions={
          canWrite ? (
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus /> New task
            </Button>
          ) : null
        }
      />
      <div className="mb-4 flex flex-wrap gap-2">
        <Segmented
          label="Whose tasks"
          value={scope}
          onChange={(v) => set("scope", v)}
          options={[
            { value: "mine", label: "Assigned to me" },
            { value: "all", label: "Everyone" },
          ]}
        />
        <Segmented
          label="Status"
          value={status}
          onChange={(v) => set("status", v)}
          options={[
            { value: "open", label: "Open" },
            { value: "done", label: "Done" },
          ]}
        />
      </div>
      <Card className="overflow-hidden">
        {tasks.isError ? (
          <ErrorState error={tasks.error} onRetry={() => void tasks.refetch()} />
        ) : !tasks.data ? (
          <div className="flex flex-col gap-2 p-4">
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : tasks.data.total === 0 ? (
          <EmptyState
            icon={<CheckSquare />}
            title={status === "open" ? "No open tasks" : "No completed tasks yet"}
            description={status === "open" ? "Create a task to plan a follow-up call, email or meeting." : undefined}
          />
        ) : (
          <>
            <ul className="divide-y divide-line">
              {tasks.data.items.map((t) => {
                const overdue = t.status === "open" && t.due_at ? new Date(t.due_at).getTime() < now : false;
                const assignee = t.assignee_user_id ? (names.get(t.assignee_user_id) ?? "Teammate") : null;
                return (
                  <li key={t.id} className="flex items-center gap-3 px-4 py-3">
                    <Checkbox
                      aria-label={t.status === "done" ? `Reopen ${t.title}` : `Complete ${t.title}`}
                      checked={t.status === "done"}
                      onCheckedChange={(v) => canWrite && toggle.mutate({ id: t.id, done: v })}
                    />
                    <div className="min-w-0 flex-1">
                      <p className={cn("truncate text-[13px] font-medium", t.status === "done" && "text-muted line-through")}>{t.title}</p>
                      <p className="truncate text-[12px] text-muted">
                        {t.lead_id ? (
                          <Link to={`/leads/${t.lead_id}`} className="hover:text-accent">
                            Open lead
                          </Link>
                        ) : (
                          "No lead"
                        )}
                        {t.description ? ` · ${t.description}` : ""}
                      </p>
                    </div>
                    {overdue ? <Badge tone="danger">Overdue</Badge> : null}
                    <span className="hidden w-32 text-right text-[12px] text-muted sm:block" title={formatDateTime(t.due_at)}>
                      {t.status === "done" ? `Done ${formatRelative(t.completed_at)}` : t.due_at ? formatRelative(t.due_at) : "No due date"}
                    </span>
                    {assignee ? <Avatar name={assignee} className="size-6 text-[10px]" /> : null}
                  </li>
                );
              })}
            </ul>
            <Pagination page={page} pageSize={PAGE_SIZE} total={tasks.data.total} onPageChange={(p) => set("page", String(p))} />
          </>
        )}
      </Card>
      <TaskDialog open={creating} onOpenChange={setCreating} />
    </>
  );
}
