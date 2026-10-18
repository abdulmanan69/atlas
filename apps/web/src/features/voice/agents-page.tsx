import { useMutation, useQuery } from "@tanstack/react-query";
import { Bot, Plus } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Badge, Card, Checkbox, Input, Skeleton } from "@/components/ui/primitives";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { useInvalidate, useOrgId } from "@/features/queries";
import { STATUS_BADGE, vk } from "@/features/voice/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { useHasRole } from "@/lib/session";
import { cn } from "@/lib/utils";

export default function AgentsPage() {
  const o = useOrgId();
  const canManage = useHasRole("manager");
  const [archived, setArchived] = useState(false);
  const [creating, setCreating] = useState(false);
  const navigate = useNavigate();
  const agents = useQuery({
    queryKey: vk.agents(o, archived),
    queryFn: () => unwrap(api.GET("/api/v1/agents", { params: { query: { include_archived: archived } } })),
  });

  return (
    <>
      <PageHeader
        title="Agents"
        description="AI voice agents that call your leads. Edit their script safely: changes go live only when you publish."
        actions={
          canManage ? (
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus /> New agent
            </Button>
          ) : null
        }
      />
      <Card className="overflow-hidden">
        <div className="flex items-center justify-end border-b border-line px-4 py-2.5">
          <label className="flex items-center gap-2 text-[12.5px] text-muted">
            <Checkbox aria-label="Show archived agents" checked={archived} onCheckedChange={setArchived} />
            Show archived
          </label>
        </div>
        {agents.isError ? (
          <ErrorState error={agents.error} onRetry={() => void agents.refetch()} />
        ) : agents.data && agents.data.length === 0 ? (
          <EmptyState
            icon={<Bot />}
            title="Create your first agent"
            description="Start from a template, such as a lead qualifier or appointment setter, and tailor the script to your business."
            action={canManage ? <Button variant="primary" onClick={() => setCreating(true)}>New agent</Button> : null}
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Agent</Th>
                <Th>Status</Th>
                <Th>Version</Th>
                <Th>Updated</Th>
              </tr>
            </thead>
            <tbody>
              {agents.data
                ? agents.data.map((a) => {
                    const status = STATUS_BADGE[a.status];
                    return (
                      <Tr key={a.id} className="cursor-pointer" onClick={(e) => !(e.target as HTMLElement).closest("a") && void navigate(`/agents/${a.id}`)}>
                        <Td>
                          <div className="flex items-center gap-3">
                            <span className="grid size-8 shrink-0 place-items-center rounded-md bg-accent-soft text-accent-soft-ink">
                              <Bot className="size-4" aria-hidden />
                            </span>
                            <div className="min-w-0">
                              <Link to={`/agents/${a.id}`} className="block truncate font-semibold hover:text-accent">
                                {a.name}
                              </Link>
                              <p className="truncate text-[12px] text-muted">{a.description ?? (a.direction === "outbound" ? "Outbound calls" : "Inbound calls")}</p>
                            </div>
                          </div>
                        </Td>
                        <Td>
                          <span className="flex flex-wrap items-center gap-1.5">
                            <Badge tone={status.tone}>{status.label}</Badge>
                            {a.sync_status === "error" ? <Badge tone="danger">Publish failed</Badge> : null}
                          </span>
                        </Td>
                        <Td className="font-mono text-[12.5px] text-ink-2">
                          {a.published_version ? `v${a.published_version} live` : "Not published"}
                          {a.published_version !== a.latest_version ? <span className={cn("ml-2 text-warn")}>· v{a.latest_version} draft</span> : null}
                        </Td>
                        <Td className="text-[12.5px] text-muted">{formatRelative(a.updated_at)}</Td>
                      </Tr>
                    );
                  })
                : Array.from({ length: 3 }, (_, i) => (
                    <tr key={i}>
                      <Td colSpan={4}>
                        <Skeleton className="h-8" />
                      </Td>
                    </tr>
                  ))}
            </tbody>
          </Table>
        )}
      </Card>
      <NewAgentDialog open={creating} onOpenChange={setCreating} />
    </>
  );
}

function NewAgentDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const o = useOrgId();
  const invalidate = useInvalidate();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("lead_qualifier");
  const templates = useQuery({ queryKey: vk.templates(o), queryFn: () => unwrap(api.GET("/api/v1/agents/templates")), enabled: open, staleTime: Infinity });
  const create = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/agents", { body: { name: name.trim(), template, direction: "outbound" } })),
    onSuccess: (a) => {
      toast.success(`Created ${a.name}`);
      void invalidate("agents");
      onOpenChange(false);
      setName("");
      void navigate(`/agents/${a.id}`);
    },
  });
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="New agent"
      description="Pick a starting point. You can change everything before it makes a single call."
      width="max-w-2xl"
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" disabled={!name.trim()} loading={create.isPending} onClick={() => create.mutate()}>
            Create agent
          </Button>
        </>
      }
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) create.mutate();
        }}
      >
        <FormError message={create.error ? errorMessage(create.error) : null} />
        <Field label="Name" hint="Internal name, e.g. “Q4 mortgage qualifier”" required>
          <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
        </Field>
        <div role="radiogroup" aria-label="Template" className="grid gap-2.5 sm:grid-cols-3">
          {(templates.data ?? []).map((t) => (
            <button
              type="button"
              role="radio"
              aria-checked={template === t.id}
              key={t.id}
              onClick={() => setTemplate(t.id)}
              className={cn(
                "flex flex-col gap-1 rounded-lg border border-line p-3 text-left transition-colors hover:border-line-strong",
                template === t.id && "border-accent bg-accent-soft/60 ring-1 ring-accent",
              )}
            >
              <span className="text-[13px] font-semibold">{t.name}</span>
              <span className="text-[12px] leading-snug text-muted">{t.description}</span>
            </button>
          ))}
          {templates.isLoading ? Array.from({ length: 3 }, (_, i) => <Skeleton key={i} className="h-24" />) : null}
        </div>
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}
