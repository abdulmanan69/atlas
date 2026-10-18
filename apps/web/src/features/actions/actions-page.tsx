import { useMutation } from "@tanstack/react-query";
import { KeyRound, MoreHorizontal, Plus, Zap } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { MenuContent, MenuItem, MenuRoot, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { Badge, Card, Skeleton, Tooltip } from "@/components/ui/primitives";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ActionEditorDialog } from "@/features/actions/action-editor-dialog";
import { AUTH_LABEL, urlSummary, type Action } from "@/features/actions/action-schema";
import { ActionTestDialog } from "@/features/actions/action-test-dialog";
import { useActions } from "@/features/actions/queries";
import { useInvalidate } from "@/features/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { useHasRole } from "@/lib/session";

const METHOD_TONE: Record<NonNullable<Action["method"]>, "accent" | "success" | "warn" | "danger" | "neutral"> = {
  GET: "accent",
  POST: "success",
  PUT: "warn",
  PATCH: "warn",
  DELETE: "danger",
};

export default function ActionsPage() {
  const actions = useActions();
  const canEdit = useHasRole("manager");
  const [creating, setCreating] = useState(false);

  return (
    <>
      <PageHeader
        title="Actions"
        description="HTTP calls an agent can make during a conversation: look up availability, book a viewing, update a record. Attach them to steps on the agent's flow."
        actions={
          canEdit ? (
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus /> New action
            </Button>
          ) : null
        }
      />
      <Card className="overflow-hidden">
        {actions.isError ? (
          <ErrorState error={actions.error} onRetry={() => void actions.refetch()} />
        ) : actions.isLoading ? (
          <div className="flex flex-col gap-3 p-4">
            <Skeleton className="h-6" />
            <Skeleton className="h-6" />
            <Skeleton className="h-6" />
          </div>
        ) : !actions.data?.length ? (
          <EmptyState
            icon={<Zap />}
            title="No actions yet"
            description="Define an HTTP call once, with the parameters the model should fill in, then attach it to any step that needs it."
            action={canEdit ? <Button onClick={() => setCreating(true)}>New action</Button> : null}
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Action</Th>
                <Th>Function</Th>
                <Th>Request</Th>
                <Th>Authentication</Th>
                <Th>Updated</Th>
                <Th className="w-12" />
              </tr>
            </thead>
            <tbody>
              {actions.data.map((a) => (
                <ActionRow key={a.id} action={a} canEdit={canEdit} />
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <ActionEditorDialog open={creating} onOpenChange={setCreating} />
    </>
  );
}

function ActionRow({ action: a, canEdit }: { action: Action; canEdit: boolean }) {
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState(false);
  const [testing, setTesting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const { host, path } = urlSummary(a.url);
  const paramCount = (a.parameters?.length ?? 0) + (a.preset_parameters?.length ?? 0);

  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/actions/{action_id}", { params: { path: { action_id: a.id } } })),
    onSuccess: () => {
      toast.success(`Deleted ${a.name}`);
      setDeleting(false);
      void invalidate("actions");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  return (
    <Tr>
      <Td>
        <div className="flex min-w-0 flex-col">
          <span className="truncate font-medium text-ink">{a.name}</span>
          <span className="line-clamp-1 max-w-md text-[12px] text-muted" title={a.description}>
            {a.description}
          </span>
        </div>
      </Td>
      <Td>
        <code className="font-mono text-[12px] text-ink-2">{a.function_name}</code>
        {paramCount ? <span className="ml-2 text-[11.5px] text-muted">{paramCount} {paramCount === 1 ? "param" : "params"}</span> : null}
      </Td>
      <Td>
        <div className="flex items-center gap-2">
          <Badge tone={METHOD_TONE[a.method ?? "POST"]} className="font-mono">
            {a.method ?? "POST"}
          </Badge>
          <span className="min-w-0 truncate font-mono text-[12px]" title={a.url}>
            <span className="text-ink-2">{host}</span>
            <span className="text-muted">{path}</span>
          </span>
        </div>
      </Td>
      <Td>
        {a.auth ? (
          <Tooltip content={a.auth.header_name ? `Sent in the ${a.auth.header_name} header` : "Sent in the Authorization header"}>
            <span className="inline-flex items-center gap-1.5 text-[12.5px] text-ink-2">
              <KeyRound className="size-3.5 text-muted" aria-hidden />
              {AUTH_LABEL[a.auth.type]}
            </span>
          </Tooltip>
        ) : (
          <span className="text-[12.5px] text-faint">None</span>
        )}
      </Td>
      <Td className="whitespace-nowrap text-muted">{formatRelative(a.updated_at ?? a.created_at)}</Td>
      <Td>
        {canEdit ? (
          <MenuRoot>
            <MenuTrigger asChild>
              <Button size="icon-sm" variant="ghost" aria-label={`Actions for ${a.name}`}>
                <MoreHorizontal />
              </Button>
            </MenuTrigger>
            <MenuContent>
              <MenuItem onSelect={() => setTesting(true)}>Test with sample values</MenuItem>
              <MenuItem onSelect={() => setEditing(true)}>Edit</MenuItem>
              <MenuSeparator />
              <MenuItem tone="danger" onSelect={() => setDeleting(true)}>
                Delete
              </MenuItem>
            </MenuContent>
          </MenuRoot>
        ) : null}
        {editing ? <ActionEditorDialog open onOpenChange={(o) => !o && setEditing(false)} existing={a} /> : null}
        <ActionTestDialog action={testing ? a : null} onOpenChange={(o) => !o && setTesting(false)} />
        <ConfirmDialog
          open={deleting}
          onOpenChange={setDeleting}
          title={`Delete ${a.name}?`}
          description={
            <>
              Steps that still reference <code className="font-mono">{a.function_name}</code> fail validation until the reference is removed and the agent is published again. The stored credential is deleted with it.
            </>
          }
          confirmLabel="Delete action"
          loading={remove.isPending}
          onConfirm={() => remove.mutate()}
        />
      </Td>
    </Tr>
  );
}
