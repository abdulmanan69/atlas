import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { KeyRound, Plus, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { EmptyState, ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { DateTimePicker } from "@/components/ui/date-time-picker";
import { ConfirmDialog, Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Badge, Card, CardHeader, Checkbox, Input, Skeleton, Tooltip } from "@/components/ui/primitives";
import { FormSelect, SelectItem } from "@/components/ui/select";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { CopyField } from "@/features/developer/copy-button";
import { keyState, useApiKeys, type ApiKey, type ApiKeyCreated } from "@/features/developer/queries";
import { useInvalidate, useMemberNames } from "@/features/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { formatDate, formatDateTime, formatRelative } from "@/lib/format";
import { useCurrentMembership, type Role } from "@/lib/session";

const ROLES: { value: Role; label: string; description: string }[] = [
  { value: "viewer", label: "Viewer", description: "Read-only: reporting and exports" },
  { value: "agent", label: "Agent", description: "Work leads, notes and tasks" },
  { value: "manager", label: "Manager", description: "CRM sync, agents, campaigns, imports" },
  { value: "admin", label: "Admin", description: "Automation that manages settings" },
  { value: "owner", label: "Owner", description: "Everything, including organization settings" },
];
const RANK: Record<Role, number> = { viewer: 0, agent: 1, manager: 2, admin: 3, owner: 4 };

export function ApiKeysTab() {
  const [showRevoked, setShowRevoked] = useState(false);
  const keys = useApiKeys(showRevoked);
  const [creating, setCreating] = useState(false);

  return (
    <>
      <Card className="overflow-hidden">
        <CardHeader
          title="API keys"
          description="Let your own systems call the API without a user signing in. Each key acts as one role in this organization."
          action={
            <div className="flex items-center gap-3">
              <label className="flex items-center gap-2 text-[12.5px] text-muted">
                <Checkbox aria-label="Show revoked keys" checked={showRevoked} onCheckedChange={setShowRevoked} />
                Show revoked
              </label>
              <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
                <Plus /> Create key
              </Button>
            </div>
          }
        />
        {keys.isError ? (
          <ErrorState error={keys.error} onRetry={() => void keys.refetch()} />
        ) : keys.isLoading ? (
          <div className="flex flex-col gap-3 p-4">
            <Skeleton className="h-6" />
            <Skeleton className="h-6" />
          </div>
        ) : !keys.data?.length ? (
          <EmptyState
            icon={<KeyRound />}
            title={showRevoked ? "No keys" : "No active keys"}
            description="Create one per integration so each can be revoked on its own. Keys are for server-to-server use; never put one in a browser or mobile app."
            action={<Button onClick={() => setCreating(true)}>Create key</Button>}
            className="py-10"
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Key</Th>
                <Th>Role</Th>
                <Th>Created</Th>
                <Th>Last used</Th>
                <Th>Expires</Th>
                <Th>Status</Th>
                <Th className="w-24" />
              </tr>
            </thead>
            <tbody>
              {keys.data.map((k) => (
                <KeyRow key={k.id} apiKey={k} />
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <CreateKeyDialog open={creating} onOpenChange={setCreating} />
    </>
  );
}

function KeyRow({ apiKey: k }: { apiKey: ApiKey }) {
  const invalidate = useInvalidate();
  const names = useMemberNames();
  const [revoking, setRevoking] = useState(false);
  const state = keyState(k);
  const revoke = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/developer/api-keys/{key_id}", { params: { path: { key_id: k.id } } })),
    onSuccess: () => {
      toast.success(`Revoked ${k.name}`);
      setRevoking(false);
      void invalidate("developer");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const creator = k.created_by_user_id ? names.get(k.created_by_user_id) : null;

  return (
    <Tr className={state !== "active" ? "text-muted" : undefined}>
      <Td>
        <div className="flex flex-col">
          <span className="font-medium text-ink">{k.name}</span>
          {creator ? <span className="text-[12px] text-muted">by {creator}</span> : null}
        </div>
      </Td>
      <Td>
        <code className="font-mono text-[12.5px] text-ink-2">{k.key_prefix}…</code>
      </Td>
      <Td className="capitalize">{k.role}</Td>
      <Td className="whitespace-nowrap">{formatDate(k.created_at)}</Td>
      <Td className="whitespace-nowrap" title={k.last_used_at ? formatDateTime(k.last_used_at) : undefined}>
        {k.last_used_at ? formatRelative(k.last_used_at) : <span className="text-faint">Never</span>}
      </Td>
      <Td className="whitespace-nowrap" title={k.expires_at ? formatDateTime(k.expires_at) : undefined}>
        {k.expires_at ? formatDate(k.expires_at) : <span className="text-faint">Never</span>}
      </Td>
      <Td>
        {state === "active" ? (
          <Badge tone="success">Active</Badge>
        ) : state === "expired" ? (
          <Badge tone="warn">Expired</Badge>
        ) : (
          <Tooltip content={`Revoked ${formatDateTime(k.revoked_at)}`}>
            <Badge tone="neutral">Revoked</Badge>
          </Tooltip>
        )}
      </Td>
      <Td className="text-right">
        {state !== "revoked" ? (
          <Button size="sm" variant="ghost" className="text-danger hover:text-danger" onClick={() => setRevoking(true)}>
            Revoke
          </Button>
        ) : null}
        <ConfirmDialog
          open={revoking}
          onOpenChange={setRevoking}
          title={`Revoke ${k.name}?`}
          description="Requests using this key fail immediately. This can't be undone; create a new key if the integration still needs access."
          confirmLabel="Revoke key"
          loading={revoke.isPending}
          onConfirm={() => revoke.mutate()}
        />
      </Td>
    </Tr>
  );
}

const schema = z.object({
  name: z.string().trim().min(1, "Give the key a name, such as the system that will use it").max(120, "At most 120 characters"),
  role: z.enum(["owner", "admin", "manager", "agent", "viewer"]),
  expires_at: z.string(),
});
type Values = z.infer<typeof schema>;

const EXPIRY_PRESETS: { label: string; days: number | null }[] = [
  { label: "30 days", days: 30 },
  { label: "90 days", days: 90 },
  { label: "1 year", days: 365 },
  { label: "No expiry", days: null },
];

function CreateKeyDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const myRole = useCurrentMembership()?.role ?? "viewer";
  const invalidate = useInvalidate();
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { name: "", role: "manager", expires_at: "" } });
  const create = useMutation({
    mutationFn: (v: Values) => unwrap(api.POST("/api/v1/developer/api-keys", { body: { name: v.name, role: v.role, expires_at: v.expires_at || null } })),
    onSuccess: (k) => {
      setCreated(k);
      void invalidate("developer");
    },
  });
  const close = (o: boolean) => {
    onOpenChange(o);
    if (!o) {
      setCreated(null);
      form.reset();
      create.reset();
    }
  };
  const submit = form.handleSubmit((v) => create.mutate(v));
  const roleHint = ROLES.find((r) => r.value === form.watch("role"))?.description;

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={created ? `${created.name} is ready` : "Create an API key"}
      description={created ? "Store it in a secret manager now." : "The key is shown once, right after it is created."}
      footer={
        created ? (
          <Button variant="primary" onClick={() => close(false)}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={() => close(false)}>Cancel</Button>
            <Button variant="primary" loading={create.isPending} onClick={() => void submit()}>
              Create key
            </Button>
          </>
        )
      }
    >
      {created ? (
        <div className="flex flex-col gap-4">
          <CopyField value={created.key} label="API key" />
          <div role="alert" className="flex items-start gap-2 rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">
            <TriangleAlert className="mt-px size-4 shrink-0" aria-hidden />
            <span>
              <span className="font-semibold">You won't see this key again.</span> ATLAS keeps only a hash and shows <code className="font-mono">{created.key_prefix}…</code> from now on. If you lose it, revoke it and create another.
            </span>
          </div>
          <dl className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-1 text-[12.5px]">
            <dt className="text-muted">Send as</dt>
            <dd>
              <code className="font-mono">X-API-Key: {created.key_prefix}…</code>
            </dd>
            <dt className="text-muted">Role</dt>
            <dd className="capitalize">{created.role}</dd>
            <dt className="text-muted">Expires</dt>
            <dd>{created.expires_at ? formatDateTime(created.expires_at) : "Never (valid until revoked)"}</dd>
          </dl>
        </div>
      ) : (
        <form className="flex flex-col gap-4" noValidate onSubmit={submit}>
          <FormError message={create.error ? errorMessage(create.error) : null} />
          <Field label="Name" required error={form.formState.errors.name?.message} hint="How it appears here and in the audit log, for example “Zapier sync”.">
            <Input autoFocus {...form.register("name")} maxLength={120} autoComplete="off" />
          </Field>
          <Field label="Role" hint={roleHint} error={form.formState.errors.role?.message}>
            <FormSelect control={form.control} name="role">
              {ROLES.filter((r) => RANK[r.value] <= RANK[myRole]).map((r) => (
                <SelectItem key={r.value} value={r.value} description={r.description}>
                  {r.label}
                </SelectItem>
              ))}
            </FormSelect>
          </Field>
          <Controller
            control={form.control}
            name="expires_at"
            render={({ field }) => (
              <Field label="Expires" hint="Set one for contractors and one-off migrations. Without an expiry the key stays valid until revoked.">
                <div className="flex flex-col gap-2">
                  <DateTimePicker value={field.value || null} onChange={(iso) => field.onChange(iso ?? "")} clearable presets={false} placeholder="Never" aria-label="Expiry" />
                  <div className="flex flex-wrap gap-1">
                    {EXPIRY_PRESETS.map((p) => (
                      <Button
                        key={p.label}
                        size="sm"
                        variant="ghost"
                        className="h-6 px-2 text-[12px]"
                        onClick={() => {
                          if (p.days === null) return field.onChange("");
                          const d = new Date();
                          d.setDate(d.getDate() + p.days);
                          field.onChange(d.toISOString());
                        }}
                      >
                        {p.label}
                      </Button>
                    ))}
                  </div>
                </div>
              </Field>
            )}
          />
          <p className="text-[12px] text-muted">The key's permissions are capped at your own role, now and later: if your role is reduced, keys you created are reduced with it.</p>
          <button type="submit" hidden />
        </form>
      )}
    </Dialog>
  );
}
