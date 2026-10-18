import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, ChevronDown, ChevronRight, CircleX, MoreHorizontal, Plus, RotateCw, TriangleAlert, Webhook as WebhookIcon } from "lucide-react";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { EmptyState, ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { ConfirmDialog, Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { MenuContent, MenuItem, MenuRoot, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { Badge, Card, CardHeader, Input, Label, Skeleton, Tooltip } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { Switch } from "@/components/ui/switch";
import { Pagination, Table, Td, Th, Tr } from "@/components/ui/table";
import { CopyField } from "@/features/developer/copy-button";
import { Drawer } from "@/features/developer/drawer";
import { useDeliveries, useEventTypes, useWebhooks, WILDCARD, type Delivery, type DeliveryStatus, type Webhook } from "@/features/developer/queries";
import { useInvalidate } from "@/features/queries";
import { api, ApiError, errorMessage, unwrap, type Schemas } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

export function WebhooksTab() {
  const hooks = useWebhooks();
  const [creating, setCreating] = useState(false);
  return (
    <>
      <Card className="overflow-hidden">
        <CardHeader
          title="Webhook endpoints"
          description="Signed HTTPS POSTs to your systems when something happens: a lead is created, moves stage, a call completes."
          action={
            <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
              <Plus /> Add endpoint
            </Button>
          }
        />
        {hooks.isError ? (
          <ErrorState error={hooks.error} onRetry={() => void hooks.refetch()} />
        ) : hooks.isLoading ? (
          <div className="flex flex-col gap-3 p-4">
            <Skeleton className="h-6" />
            <Skeleton className="h-6" />
          </div>
        ) : !hooks.data?.length ? (
          <EmptyState
            icon={<WebhookIcon />}
            title="No endpoints yet"
            description="Register an HTTPS URL on a public host and choose the events it should receive. Every delivery is signed; the Docs tab shows how to verify it."
            action={<Button onClick={() => setCreating(true)}>Add endpoint</Button>}
            className="py-10"
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Endpoint</Th>
                <Th>Events</Th>
                <Th>Active</Th>
                <Th>Health</Th>
                <Th>Updated</Th>
                <Th className="w-12" />
              </tr>
            </thead>
            <tbody>
              {hooks.data.map((w) => (
                <WebhookRow key={w.id} hook={w} />
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <EndpointDialog open={creating} onOpenChange={setCreating} />
    </>
  );
}

function WebhookRow({ hook: w }: { hook: Webhook }) {
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState(false);
  const [deliveries, setDeliveries] = useState(false);
  const [rotating, setRotating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [test, setTest] = useState<Schemas["WebhookTestResult"] | null>(null);

  const toggle = useMutation({
    mutationFn: (is_active: boolean) => unwrap(api.PATCH("/api/v1/developer/webhooks/{endpoint_id}", { params: { path: { endpoint_id: w.id } }, body: { is_active } })),
    onSuccess: (u) => {
      toast.success(u.is_active ? "Endpoint enabled. Parked deliveries resume." : "Endpoint paused. Events that occur now are not queued for it.");
      void invalidate("developer");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const runTest = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/developer/webhooks/{endpoint_id}/test", { params: { path: { endpoint_id: w.id } } })),
    onSuccess: setTest,
    onError: (e) => toast.error(errorMessage(e)),
  });
  const rotate = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/developer/webhooks/{endpoint_id}/rotate-secret", { params: { path: { endpoint_id: w.id } } })),
    onSuccess: (r) => {
      setRotating(false);
      setSecret(r.secret);
      void invalidate("developer");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/developer/webhooks/{endpoint_id}", { params: { path: { endpoint_id: w.id } } })),
    onSuccess: () => {
      toast.success("Endpoint deleted");
      setDeleting(false);
      void invalidate("developer");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const all = w.events.includes(WILDCARD);
  const disabledByFailures = !w.is_active && Boolean(w.disabled_reason);

  return (
    <Tr className={!w.is_active ? "text-muted" : undefined}>
      <Td>
        <div className="flex min-w-0 flex-col">
          <code className="truncate font-mono text-[12.5px] text-ink" title={w.url}>
            {w.url}
          </code>
          {w.description ? <span className="truncate text-[12px] text-muted">{w.description}</span> : null}
        </div>
      </Td>
      <Td>
        <div className="flex max-w-xs flex-wrap gap-1">
          {all ? (
            <Badge tone="accent">All events</Badge>
          ) : (
            w.events.map((e) => (
              <Badge key={e} className="font-mono">
                {e}
              </Badge>
            ))
          )}
        </div>
      </Td>
      <Td>
        <Switch aria-label={`${w.is_active ? "Pause" : "Enable"} ${w.url}`} checked={w.is_active} onCheckedChange={(v) => toggle.mutate(v)} disabled={toggle.isPending} size="sm" />
      </Td>
      <Td>
        {disabledByFailures ? (
          <Tooltip content={`${w.disabled_reason}${w.disabled_at ? ` · ${formatDateTime(w.disabled_at)}` : ""}`}>
            <Badge tone="danger" className="cursor-help">
              Disabled automatically
            </Badge>
          </Tooltip>
        ) : w.failure_count > 0 ? (
          <Tooltip content="Consecutive failed deliveries. Resets on the next success; 50 in a row disables the endpoint.">
            <Badge tone="warn" className="cursor-help">
              {w.failure_count} failing
            </Badge>
          </Tooltip>
        ) : (
          <Badge tone="success">Healthy</Badge>
        )}
      </Td>
      <Td className="whitespace-nowrap" title={formatDateTime(w.updated_at)}>
        {formatRelative(w.updated_at)}
      </Td>
      <Td>
        <MenuRoot>
          <MenuTrigger asChild>
            <Button size="icon-sm" variant="ghost" aria-label={`Actions for ${w.url}`}>
              <MoreHorizontal />
            </Button>
          </MenuTrigger>
          <MenuContent>
            <MenuItem onSelect={() => setDeliveries(true)}>Delivery history</MenuItem>
            <MenuItem onSelect={() => runTest.mutate()}>Send test ping</MenuItem>
            <MenuSeparator />
            <MenuItem onSelect={() => setEditing(true)}>Edit</MenuItem>
            <MenuItem onSelect={() => setRotating(true)}>Rotate secret</MenuItem>
            <MenuSeparator />
            <MenuItem tone="danger" onSelect={() => setDeleting(true)}>
              Delete
            </MenuItem>
          </MenuContent>
        </MenuRoot>

        {editing ? <EndpointDialog open onOpenChange={(o) => !o && setEditing(false)} existing={w} /> : null}
        <DeliveriesDrawer hook={deliveries ? w : null} onOpenChange={(o) => !o && setDeliveries(false)} />
        <SecretDialog secret={secret} title="New signing secret" onClose={() => setSecret(null)} rotated />
        <TestResultDialog result={test} url={w.url} onClose={() => setTest(null)} />
        <ConfirmDialog
          open={rotating}
          onOpenChange={setRotating}
          title="Rotate the signing secret?"
          description="The current secret stops working the moment the new one is issued. Update your receiver to accept the new secret first, then rotate."
          confirmLabel="Rotate secret"
          tone="primary"
          loading={rotate.isPending}
          onConfirm={() => rotate.mutate()}
        />
        <ConfirmDialog
          open={deleting}
          onOpenChange={setDeleting}
          title="Delete this endpoint?"
          description={
            <>
              <code className="font-mono">{w.url}</code> stops receiving events and its delivery history is removed.
            </>
          }
          confirmLabel="Delete endpoint"
          loading={remove.isPending}
          onConfirm={() => remove.mutate()}
        />
      </Td>
    </Tr>
  );
}

const endpointSchema = z.object({
  url: z.string().trim().min(1, "URL is required").max(2048),
  description: z.string().trim().max(200, "At most 200 characters"),
  all: z.boolean(),
  events: z.array(z.string()),
});
type EndpointValues = z.infer<typeof endpointSchema>;

function urlProblem(raw: string): string | null {
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return "Enter a full URL, such as https://hooks.example.com/atlas";
  }
  if (u.protocol !== "https:") return "Webhook URLs must use https://";
  if (u.username || u.password) return "Remove credentials from the URL";
  return null;
}

function EndpointDialog({ open, onOpenChange, existing }: { open: boolean; onOpenChange: (open: boolean) => void; existing?: Webhook }) {
  const invalidate = useInvalidate();
  const events = useEventTypes();
  const [created, setCreated] = useState<Schemas["WebhookEndpointCreated"] | null>(null);
  const form = useForm<EndpointValues>({
    resolver: zodResolver(
      endpointSchema.superRefine((v, ctx) => {
        const p = urlProblem(v.url);
        if (p) ctx.addIssue({ code: "custom", path: ["url"], message: p });
        if (!v.all && v.events.length === 0) ctx.addIssue({ code: "custom", path: ["events"], message: "Choose at least one event, or all events" });
      }),
    ),
    defaultValues: {
      url: existing?.url ?? "",
      description: existing?.description ?? "",
      all: existing?.events.includes(WILDCARD) ?? false,
      events: existing?.events.filter((e) => e !== WILDCARD) ?? [],
    },
  });
  const save = useMutation({
    mutationFn: (v: EndpointValues) => {
      const body = { url: v.url, description: v.description || null, events: v.all ? [WILDCARD] : v.events };
      return existing
        ? unwrap(api.PATCH("/api/v1/developer/webhooks/{endpoint_id}", { params: { path: { endpoint_id: existing.id } }, body })).then(() => null)
        : unwrap(api.POST("/api/v1/developer/webhooks", { body }));
    },
    onSuccess: (r) => {
      void invalidate("developer");
      if (r) setCreated(r);
      else {
        toast.success("Endpoint updated");
        close(false);
      }
    },
    onError: (e) => {
      if (e instanceof ApiError && typeof e.details === "object" && e.details && (e.details as { field?: string }).field === "url") form.setError("url", { message: e.message });
    },
  });
  const close = (o: boolean) => {
    onOpenChange(o);
    if (!o) {
      setCreated(null);
      form.reset();
      save.reset();
    }
  };
  const submit = form.handleSubmit((v) => save.mutate(v));
  const all = form.watch("all");
  const selectable = (events.data ?? []).filter((e) => e.type !== WILDCARD);
  const wildcard = events.data?.find((e) => e.type === WILDCARD);

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={created ? "Endpoint registered" : existing ? "Edit endpoint" : "Add a webhook endpoint"}
      description={created ? "Use this secret to verify every delivery. It is shown once." : "https:// on a public host. Private, loopback and cloud-metadata addresses are refused, here and before every delivery."}
      width="max-w-xl"
      footer={
        created ? (
          <Button variant="primary" onClick={() => close(false)}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={() => close(false)}>Cancel</Button>
            <Button variant="primary" loading={save.isPending} onClick={() => void submit()}>
              {existing ? "Save endpoint" : "Register endpoint"}
            </Button>
          </>
        )
      }
    >
      {created ? (
        <SecretBody secret={created.secret} />
      ) : (
        <form className="flex flex-col gap-4" noValidate onSubmit={submit}>
          <FormError message={save.error ? errorMessage(save.error) : null} />
          <Field label="URL" required error={form.formState.errors.url?.message} hint="Redirects are not followed; a 3xx counts as a failure. Respond 2xx within 10 seconds.">
            <Input {...form.register("url")} className="font-mono" placeholder="https://hooks.example.com/atlas" spellCheck={false} autoComplete="off" autoFocus={!existing} />
          </Field>
          <Field label="Description" error={form.formState.errors.description?.message} hint="For your team, for example “CRM sync”.">
            <Input {...form.register("description")} maxLength={200} />
          </Field>
          <div className="flex flex-col gap-2">
            <Label>Events</Label>
            <div className="divide-y divide-line rounded-lg border border-line">
              <Controller
                control={form.control}
                name="all"
                render={({ field }) => (
                  <label className="flex cursor-pointer items-start justify-between gap-4 px-3 py-2.5">
                    <span>
                      <span className="block text-[13px] font-medium text-ink">All events</span>
                      <span className="block text-[12px] text-muted">{wildcard?.description ?? "Every event type, including ones added later."}</span>
                    </span>
                    <Switch checked={field.value} onCheckedChange={field.onChange} aria-label="All events" />
                  </label>
                )}
              />
              <Controller
                control={form.control}
                name="events"
                render={({ field }) => (
                  <div className={cn("divide-y divide-line transition-opacity", all && "pointer-events-none opacity-40")} aria-disabled={all}>
                    {events.isLoading ? (
                      <div className="p-3">
                        <Skeleton className="h-16" />
                      </div>
                    ) : (
                      selectable.map((e) => {
                        const on = field.value.includes(e.type);
                        return (
                          <label key={e.type} className="flex cursor-pointer items-start justify-between gap-4 px-3 py-2">
                            <span>
                              <code className="block font-mono text-[12.5px] text-ink">{e.type}</code>
                              <span className="block text-[12px] text-muted">{e.description}</span>
                            </span>
                            <Switch size="sm" checked={all || on} disabled={all} onCheckedChange={(v) => field.onChange(v ? [...field.value, e.type] : field.value.filter((x) => x !== e.type))} aria-label={e.type} />
                          </label>
                        );
                      })
                    )}
                  </div>
                )}
              />
            </div>
            {form.formState.errors.events?.message ? (
              <p role="alert" className="text-[12px] text-danger">
                {form.formState.errors.events.message}
              </p>
            ) : null}
          </div>
          <button type="submit" hidden />
        </form>
      )}
    </Dialog>
  );
}

function SecretBody({ secret, rotated }: { secret: string; rotated?: boolean }) {
  return (
    <div className="flex flex-col gap-4">
      <CopyField value={secret} label="Signing secret" />
      <div role="alert" className="flex items-start gap-2 rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">
        <TriangleAlert className="mt-px size-4 shrink-0" aria-hidden />
        <span>
          <span className="font-semibold">You won't see this secret again.</span> {rotated ? "The previous secret has already stopped working." : "Store it with your receiver; the Docs tab shows how to verify signatures with it."}
        </span>
      </div>
    </div>
  );
}

function SecretDialog({ secret, title, onClose, rotated }: { secret: string | null; title: string; onClose: () => void; rotated?: boolean }) {
  return (
    <Dialog
      open={secret !== null}
      onOpenChange={(o) => !o && onClose()}
      title={title}
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      {secret ? <SecretBody secret={secret} rotated={rotated} /> : null}
    </Dialog>
  );
}

function TestResultDialog({ result, url, onClose }: { result: Schemas["WebhookTestResult"] | null; url: string; onClose: () => void }) {
  return (
    <Dialog
      open={result !== null}
      onOpenChange={(o) => !o && onClose()}
      title="Test ping"
      description={`A signed ping event was sent to ${url}.`}
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      {result ? (
        <div role="status" className={cn("flex flex-col gap-2 rounded-lg border border-line p-3 text-[13px]", result.ok ? "text-success" : "text-danger")}>
          <div className="flex items-center gap-2 font-semibold">
            {result.ok ? <CheckCircle2 className="size-4" aria-hidden /> : <CircleX className="size-4" aria-hidden />}
            {result.ok ? "Receiver accepted the ping" : "Receiver didn't accept the ping"}
            <span className="ml-auto font-mono text-[12px] font-normal text-ink-2">{result.status_code !== null ? `HTTP ${result.status_code}` : "No response"}</span>
            <span className="font-mono text-[12px] font-normal text-muted">{result.latency_ms} ms</span>
          </div>
          {result.error ? <p className="text-[12.5px]">{result.error}</p> : null}
          {!result.ok ? <p className="text-[12px] text-muted">Any 2xx within 10 seconds counts as delivered. Check the signature verification and that the host is reachable from the internet.</p> : null}
        </div>
      ) : null}
    </Dialog>
  );
}

const STATUS_FILTERS: { value: "all" | DeliveryStatus; label: string }[] = [
  { value: "all", label: "All" },
  { value: "pending", label: "Pending" },
  { value: "delivered", label: "Delivered" },
  { value: "failed", label: "Failed" },
  { value: "dead", label: "Dead" },
];
const DELIVERY_TONE: Record<DeliveryStatus, "neutral" | "success" | "warn" | "danger"> = { pending: "neutral", delivered: "success", failed: "warn", dead: "danger" };
const PAGE_SIZE = 25;

function DeliveriesDrawer({ hook, onOpenChange }: { hook: Webhook | null; onOpenChange: (open: boolean) => void }) {
  const [status, setStatus] = useState<"all" | DeliveryStatus>("all");
  const [page, setPage] = useState(1);
  const query = { status: status === "all" ? undefined : status, page, page_size: PAGE_SIZE };
  const deliveries = useDeliveries(hook?.id ?? null, query);

  return (
    <Drawer open={hook !== null} onOpenChange={onOpenChange} title="Delivery history" description={hook ? <code className="font-mono text-[12px]">{hook.url}</code> : undefined}>
      <div className="flex items-center justify-between gap-3 border-b border-line px-5 py-2.5">
        <Segmented
          label="Delivery status"
          size="sm"
          value={status}
          options={STATUS_FILTERS}
          onChange={(v) => {
            setStatus(v);
            setPage(1);
          }}
        />
        <span className="text-[12px] text-muted">Retried 8 times over about two hours, then marked dead. Dead deliveries stay for 30 days.</span>
      </div>
      {deliveries.isError ? (
        <ErrorState error={deliveries.error} onRetry={() => void deliveries.refetch()} />
      ) : deliveries.isLoading ? (
        <div className="flex flex-col gap-3 p-5">
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
        </div>
      ) : !deliveries.data?.items.length ? (
        <EmptyState title={status === "all" ? "No deliveries yet" : `No ${status} deliveries`} description="Deliveries appear here as events happen. Send a test ping to see one now." className="py-12" />
      ) : (
        <>
          <ul className="divide-y divide-line">
            {deliveries.data.items.map((d) => (
              <DeliveryItem key={d.id} delivery={d} />
            ))}
          </ul>
          <Pagination page={page} pageSize={PAGE_SIZE} total={deliveries.data.total} onPageChange={setPage} />
        </>
      )}
    </Drawer>
  );
}

function DeliveryItem({ delivery: d }: { delivery: Delivery }) {
  const invalidate = useInvalidate();
  const [open, setOpen] = useState(false);
  const retry = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/developer/deliveries/{delivery_id}/retry", { params: { path: { delivery_id: d.id } } })),
    onSuccess: () => {
      toast.success("Queued again");
      void invalidate("developer");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const retryable = d.status === "failed" || d.status === "dead";

  return (
    <li className="px-5 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Badge tone={DELIVERY_TONE[d.status]} className="capitalize">
          {d.status}
        </Badge>
        <code className="font-mono text-[12.5px] text-ink">{d.event_type}</code>
        <span className="text-[12px] text-muted" title={formatDateTime(d.created_at)}>
          {formatRelative(d.created_at)}
        </span>
        <span className="ml-auto flex items-center gap-2 text-[12px] text-muted">
          {d.last_status_code !== null ? <span className="font-mono">HTTP {d.last_status_code}</span> : null}
          <span>
            {d.attempts} {d.attempts === 1 ? "attempt" : "attempts"}
          </span>
          {retryable ? (
            <Button size="sm" variant="ghost" loading={retry.isPending} onClick={() => retry.mutate()}>
              <RotateCw /> Retry
            </Button>
          ) : null}
        </span>
      </div>
      {d.last_error ? <p className="mt-1 text-[12px] text-danger">{d.last_error}</p> : null}
      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-0.5 text-[11.5px] text-muted">
        {d.status === "pending" && d.next_attempt_at ? <span>Next attempt {formatRelative(d.next_attempt_at)}</span> : null}
        {d.delivered_at ? <span>Delivered {formatDateTime(d.delivered_at)}</span> : null}
        <span className="font-mono">event {d.event_id}</span>
      </div>
      <button type="button" className="mt-1.5 inline-flex items-center gap-1 text-[12px] text-muted hover:text-ink" onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        Payload
      </button>
      {open ? <pre className="mt-1.5 max-h-72 overflow-auto rounded-md border border-line bg-surface-2/60 p-2.5 font-mono text-[11.5px] leading-relaxed text-ink-2">{JSON.stringify(d.payload, null, 2)}</pre> : null}
    </li>
  );
}
