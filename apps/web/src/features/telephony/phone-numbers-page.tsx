import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Cloud, MoreHorizontal, Phone, PhoneIncoming, Plus, RefreshCw, Server, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { ConfirmDialog, Dialog } from "@/components/ui/dialog";
import { FormError } from "@/components/ui/form";
import { MenuContent, MenuItem, MenuRoot, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { Badge, Card, CardHeader, Skeleton, Tooltip } from "@/components/ui/primitives";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { useInvalidate, useOrgId } from "@/features/queries";
import { AddNumberDialog } from "@/features/telephony/number-dialog";
import { ProviderDialog } from "@/features/telephony/provider-dialog";
import { CONNECTIVITY_LABEL, tk, useNumbers, useProviders, usePublishedAgents, useTelephonyCatalog, type NumberSyncResult, type PhoneNumber, type Provider } from "@/features/telephony/queries";
import { SetupInstructionsDialog } from "@/features/telephony/setup-dialog";
import { api, errorMessage, unwrap } from "@/lib/api";
import { useHasRole } from "@/lib/session";

const NO_AGENT = "No agent — inbound calls not answered";

export default function PhoneNumbersPage() {
  const catalog = useTelephonyCatalog();
  const providers = useProviders();
  const numbers = useNumbers();
  const canManage = useHasRole("admin");
  const [connecting, setConnecting] = useState(false);
  const [adding, setAdding] = useState<string | null | false>(false);
  const [setup, setSetup] = useState<Provider | null>(null);

  const error = providers.error ?? numbers.error;
  const list = providers.data ?? [];

  return (
    <>
      <PageHeader
        title="Phone numbers"
        description="Calling providers and the numbers your agents answer and call from. Credentials are kept by the voice service; this page shows which fields are on file."
        actions={
          canManage ? (
            <>
              {list.length ? (
                <Button onClick={() => setAdding(null)}>
                  <Phone /> Add number
                </Button>
              ) : null}
              <Button variant="primary" onClick={() => setConnecting(true)}>
                <Plus /> Connect provider
              </Button>
            </>
          ) : null
        }
      />
      {error ? (
        <Card>
          <ErrorState error={error} onRetry={() => void Promise.all([providers.refetch(), numbers.refetch()])} />
        </Card>
      ) : providers.isLoading ? (
        <div className="flex flex-col gap-5">
          <Skeleton className="h-28" />
          <Skeleton className="h-48" />
        </div>
      ) : list.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Phone />}
            title="Connect a provider to make and receive calls."
            description="Agents answer and place calls over a calling provider account you own: Twilio, Vonage, Plivo, Telnyx or your own SIP trunk. Connect one, add its numbers, then choose which agent answers each."
            action={canManage ? <Button variant="primary" onClick={() => setConnecting(true)}>Connect provider</Button> : <span className="text-[12.5px] text-muted">Ask an administrator to connect one.</span>}
          />
        </Card>
      ) : (
        <div className="flex flex-col gap-5">
          <section aria-label="Providers" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {list.map((p) => (
              <ProviderCard key={p.id} provider={p} canManage={canManage} onSetup={() => setSetup(p)} onAddNumber={() => setAdding(p.id)} />
            ))}
          </section>
          <NumbersCard numbers={numbers.data} loading={numbers.isLoading} providers={list} canManage={canManage} onAdd={() => setAdding(null)} onSetup={setSetup} />
        </div>
      )}

      <ProviderDialog open={connecting} onOpenChange={setConnecting} catalog={catalog.data} />
      {adding !== false ? <AddNumberDialog open onOpenChange={(o) => !o && setAdding(false)} providers={list} initialProviderId={adding ?? undefined} /> : null}
      <SetupInstructionsDialog provider={setup} onOpenChange={(o) => !o && setSetup(null)} />
    </>
  );
}

function ProviderCard({ provider: p, canManage, onSetup, onAddNumber }: { provider: Provider; canManage: boolean; onSetup: () => void; onAddNumber: () => void }) {
  const catalog = useTelephonyCatalog();
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [syncResult, setSyncResult] = useState<NumberSyncResult | null>(null);
  const fieldLabel = (name: string) => catalog.data?.find((o) => o.id === p.provider)?.fields.find((f) => f.name === name)?.label ?? name;

  const setDefault = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/telephony/providers/{provider_id}/set-default-outbound", { params: { path: { provider_id: p.id } } })),
    onSuccess: () => {
      toast.success(`${p.name} now places outbound calls`);
      void invalidate("telephony");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const sync = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/telephony/providers/{provider_id}/numbers/sync", { params: { path: { provider_id: p.id } } })),
    onSuccess: (r) => {
      setSyncResult(r);
      void invalidate("telephony");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/telephony/providers/{provider_id}", { params: { path: { provider_id: p.id } } })),
    onSuccess: () => {
      toast.success(`Disconnected ${p.name}`);
      setRemoving(false);
      void invalidate("telephony");
    },
  });

  const Icon = p.connectivity === "sip" ? Server : Cloud;
  const warning = p.inactive ? (p.inactive_reason ?? "This provider is inactive.") : !p.ready_for_outbound && p.supports_outbound ? p.outbound_blocked_reason : null;

  return (
    <Card className="flex flex-col">
      <div className="flex items-start gap-3 px-4 pt-4">
        <span className="grid size-9 shrink-0 place-items-center rounded-md bg-surface-2 text-muted">
          <Icon className="size-4.5" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13.5px] font-semibold text-ink">{p.name}</p>
          <p className="truncate text-[12px] text-muted">
            {p.provider_title} · {CONNECTIVITY_LABEL[p.connectivity ?? "api"]}
          </p>
        </div>
        {canManage ? (
          <MenuRoot>
            <MenuTrigger asChild>
              <Button size="icon-sm" variant="ghost" aria-label={`Actions for ${p.name}`}>
                <MoreHorizontal />
              </Button>
            </MenuTrigger>
            <MenuContent>
              <MenuItem onSelect={onSetup}>Setup instructions</MenuItem>
              <MenuItem onSelect={onAddNumber}>Add number</MenuItem>
              {p.supports_number_sync ? <MenuItem onSelect={() => sync.mutate()}>Import numbers from account</MenuItem> : null}
              <MenuSeparator />
              <MenuItem onSelect={() => setEditing(true)}>Edit or rotate credentials</MenuItem>
              {p.supports_outbound && !p.is_default_outbound ? <MenuItem onSelect={() => setDefault.mutate()}>Use for outbound calls</MenuItem> : null}
              <MenuSeparator />
              <MenuItem tone="danger" onSelect={() => setRemoving(true)}>
                Disconnect
              </MenuItem>
            </MenuContent>
          </MenuRoot>
        ) : (
          <Button size="sm" variant="ghost" onClick={onSetup}>
            Setup
          </Button>
        )}
      </div>
      <div className="flex flex-wrap gap-1 px-4 pt-3">
        {p.is_default_outbound ? <Badge tone="accent">Default for outbound</Badge> : null}
        {p.configured_fields.map((f) => (
          <Badge key={f} tone="success">
            {fieldLabel(f)} stored
          </Badge>
        ))}
      </div>
      {warning ? (
        <p className="mx-4 mt-3 flex items-start gap-1.5 rounded-md bg-warn-soft px-2.5 py-1.5 text-[12px] text-warn">
          <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden />
          {warning}
        </p>
      ) : null}
      <div className="mt-auto flex items-center justify-between gap-3 px-4 pb-3 pt-3 text-[12.5px] text-muted">
        <span>
          <span className="font-mono text-ink-2">{p.number_count}</span> {p.number_count === 1 ? "number" : "numbers"}
        </span>
        {sync.isPending ? (
          <span className="inline-flex items-center gap-1.5">
            <RefreshCw className="size-3.5 animate-spin" aria-hidden /> Importing…
          </span>
        ) : null}
      </div>

      {editing ? <ProviderDialog open onOpenChange={(o) => !o && setEditing(false)} catalog={catalog.data} existing={p} /> : null}
      <ConfirmDialog
        open={removing}
        onOpenChange={(o) => {
          setRemoving(o);
          if (!o) remove.reset();
        }}
        title={`Disconnect ${p.name}?`}
        description={
          <span className="flex flex-col gap-3">
            <span>
              Its {p.number_count} {p.number_count === 1 ? "number stops" : "numbers stop"} being answered and agents can no longer call through this account. The credentials are deleted from the voice service.
            </span>
            <FormError message={remove.error ? errorMessage(remove.error) : null} />
          </span>
        }
        confirmLabel="Disconnect provider"
        loading={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
      <SyncResultDialog result={syncResult} providerName={p.name} onClose={() => setSyncResult(null)} />
    </Card>
  );
}

function SyncResultDialog({ result, providerName, onClose }: { result: NumberSyncResult | null; providerName: string; onClose: () => void }) {
  const total = result ? result.added.length + result.updated.length : 0;
  return (
    <Dialog
      open={result !== null}
      onOpenChange={(o) => !o && onClose()}
      title="Numbers imported"
      description={`What ${providerName} holds compared with what is on file here.`}
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      {result ? (
        <div className="flex flex-col gap-4">
          <dl className="grid grid-cols-4 divide-x divide-line rounded-lg border border-line text-center">
            {[
              ["Added", result.added.length],
              ["Updated", result.updated.length],
              ["Skipped", result.skipped.length],
              ["Missing", result.missing_at_provider.length],
            ].map(([label, value]) => (
              <div key={label} className="px-2 py-3">
                <dt className="eyebrow text-[10px]">{label}</dt>
                <dd className="mt-1 font-mono text-[18px]">{value}</dd>
              </div>
            ))}
          </dl>
          {total === 0 && result.skipped.length === 0 && result.missing_at_provider.length === 0 ? (
            <p className="text-[13px] text-muted">Everything on file already matches the account.</p>
          ) : null}
          {result.added.length ? <NumberList title="Added" items={result.added.map((n) => n.display)} hint="Assign an agent to each so it gets answered." /> : null}
          {result.skipped.length ? <NumberList title="Skipped" items={result.skipped.map((s) => `${s.phone_number} — ${s.reason}`)} /> : null}
          {result.missing_at_provider.length ? (
            <NumberList title="No longer at the provider" items={result.missing_at_provider.map((n) => n.display)} hint="Nothing was removed. Remove them here if the account really dropped them." />
          ) : null}
        </div>
      ) : null}
    </Dialog>
  );
}

function NumberList({ title, items, hint }: { title: string; items: string[]; hint?: string }) {
  return (
    <div>
      <p className="text-[12.5px] font-medium text-ink-2">{title}</p>
      {hint ? <p className="text-[12px] text-muted">{hint}</p> : null}
      <ul className="mt-1.5 max-h-40 overflow-y-auto rounded-md border border-line font-mono text-[12px]">
        {items.map((i) => (
          <li key={i} className="border-b border-line px-2.5 py-1.5 last:border-0">
            {i}
          </li>
        ))}
      </ul>
    </div>
  );
}

function NumbersCard({
  numbers,
  loading,
  providers,
  canManage,
  onAdd,
  onSetup,
}: {
  numbers: PhoneNumber[] | undefined;
  loading: boolean;
  providers: Provider[];
  canManage: boolean;
  onAdd: () => void;
  onSetup: (p: Provider) => void;
}) {
  const canAssign = useHasRole("manager");
  const agents = usePublishedAgents();
  const unanswered = numbers?.filter((n) => !n.inbound_agent_id).length ?? 0;
  return (
    <Card className="overflow-hidden">
      <CardHeader
        title="Numbers"
        description={
          numbers?.length
            ? unanswered
              ? `${unanswered} of ${numbers.length} ${unanswered === 1 ? "has" : "have"} no agent and won't be answered.`
              : "Every number has an agent answering it."
            : "Each number is answered by the published agent you choose."
        }
        action={
          agents.data && agents.data.length === 0 ? (
            <Badge tone="warn" className="h-auto whitespace-normal py-0.5">
              No published agents yet
            </Badge>
          ) : null
        }
      />
      {loading ? (
        <div className="p-4">
          <Skeleton className="h-20" />
        </div>
      ) : !numbers?.length ? (
        <EmptyState
          icon={<PhoneIncoming />}
          title="No numbers yet"
          description="Add a number the account owns, or import the numbers it holds from the provider's menu."
          action={canManage ? <Button onClick={onAdd}>Add number</Button> : null}
          className="py-10"
        />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Number</Th>
              <Th>Provider</Th>
              <Th className="w-[320px]">Inbound agent</Th>
              <Th>Provider webhook</Th>
              <Th className="w-12" />
            </tr>
          </thead>
          <tbody>
            {numbers.map((n) => (
              <NumberRow key={n.id} number={n} provider={providers.find((p) => p.id === n.provider_id)} canAssign={canAssign} canManage={canManage} agents={agents.data} agentsLoading={agents.isLoading} onSetup={onSetup} />
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

function NumberRow({
  number: n,
  provider,
  canAssign,
  canManage,
  agents,
  agentsLoading,
  onSetup,
}: {
  number: PhoneNumber;
  provider: Provider | undefined;
  canAssign: boolean;
  canManage: boolean;
  agents: ReturnType<typeof usePublishedAgents>["data"];
  agentsLoading: boolean;
  onSetup: (p: Provider) => void;
}) {
  const qc = useQueryClient();
  const o = useOrgId();
  const invalidate = useInvalidate();
  const [removing, setRemoving] = useState(false);

  const assign = useMutation({
    mutationFn: (agentId: string | null) =>
      unwrap(api.PUT("/api/v1/telephony/numbers/{number_id}/inbound-agent", { params: { path: { number_id: n.id } }, body: { agent_id: agentId } })),
    onSuccess: (updated) => {
      qc.setQueryData<PhoneNumber[]>(tk.numbers(o), (prev) => prev?.map((x) => (x.id === updated.id ? updated : x)));
      if (!updated.inbound_agent_id) toast.success(`${updated.display} no longer answers calls`);
      else if (updated.provider_sync && !updated.provider_sync.ok) toast.warning(`Assigned ${updated.inbound_agent_name}, but the provider webhook wasn't updated. ${updated.provider_sync.message ?? "Follow the setup instructions."}`);
      else toast.success(`${updated.inbound_agent_name} answers ${updated.display}`);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/telephony/numbers/{number_id}", { params: { path: { number_id: n.id } } })),
    onSuccess: () => {
      toast.success(`Removed ${n.display}`);
      setRemoving(false);
      void invalidate("telephony");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const options = (agents ?? []).map((a) => ({ value: a.id, label: a.name, description: `v${a.published_version} live · ${a.direction}` }));

  return (
    <Tr>
      <Td>
        <div className="flex flex-col">
          <span className="font-mono text-[13px] text-ink">{n.display}</span>
          {n.label ? <span className="text-[12px] text-muted">{n.label}</span> : null}
        </div>
      </Td>
      <Td>
        <span className="text-ink-2">{n.provider_name}</span>
        {n.is_default_caller_id ? (
          <Badge tone="accent" className="ml-2">
            Caller ID
          </Badge>
        ) : null}
      </Td>
      <Td>
        {canAssign ? (
          <Combobox
            aria-label={`Inbound agent for ${n.display}`}
            options={options}
            value={n.inbound_agent_id ?? null}
            valueLabel={n.inbound_agent_name ?? undefined}
            onValueChange={(v) => assign.mutate(v)}
            placeholder={NO_AGENT}
            searchPlaceholder="Search published agents…"
            emptyText="No published agents. Publish one under Agents first."
            loading={agentsLoading || assign.isPending}
            allowClear
            size="sm"
          />
        ) : n.inbound_agent_name ? (
          <span>{n.inbound_agent_name}</span>
        ) : (
          <span className="text-warn">{NO_AGENT}</span>
        )}
      </Td>
      <Td>
        <SyncStatus number={n} provider={provider} onSetup={onSetup} />
      </Td>
      <Td>
        {canManage ? (
          <MenuRoot>
            <MenuTrigger asChild>
              <Button size="icon-sm" variant="ghost" aria-label={`Actions for ${n.display}`}>
                <MoreHorizontal />
              </Button>
            </MenuTrigger>
            <MenuContent>
              {provider ? <MenuItem onSelect={() => onSetup(provider)}>Setup instructions</MenuItem> : null}
              <MenuItem tone="danger" onSelect={() => setRemoving(true)}>
                Remove number
              </MenuItem>
            </MenuContent>
          </MenuRoot>
        ) : null}
        <ConfirmDialog
          open={removing}
          onOpenChange={setRemoving}
          title={`Remove ${n.display}?`}
          description="Calls to this number are no longer answered by an agent. The number stays in your provider account."
          confirmLabel="Remove number"
          loading={remove.isPending}
          onConfirm={() => remove.mutate()}
        />
      </Td>
    </Tr>
  );
}

function SyncStatus({ number: n, provider, onSetup }: { number: PhoneNumber; provider: Provider | undefined; onSetup: (p: Provider) => void }) {
  if (provider?.connectivity === "sip") return <span className="text-[12px] text-muted">Routed over ARI</span>;
  if (!n.inbound_agent_id) return <span className="text-faint">—</span>;
  if (!n.provider_sync) {
    return (
      <Tooltip content="Checked when an agent is assigned. Reassign to re-run it.">
        <span className="text-[12px] text-muted">Not checked this session</span>
      </Tooltip>
    );
  }
  if (n.provider_sync.ok) return <Badge tone="success">Webhook set</Badge>;
  return (
    <span className="inline-flex items-center gap-1.5">
      <Tooltip content={n.provider_sync.message ?? "The provider rejected the webhook update."}>
        <Badge tone="warn">Manual setup needed</Badge>
      </Tooltip>
      {provider ? (
        <Button size="sm" variant="link" onClick={() => onSetup(provider)}>
          Instructions
        </Button>
      ) : null}
    </span>
  );
}
