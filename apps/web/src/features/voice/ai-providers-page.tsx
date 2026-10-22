import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, Cpu, KeyRound, MoreHorizontal, Plus, Server, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { FormError } from "@/components/ui/form";
import { MenuContent, MenuItem, MenuRoot, MenuTrigger } from "@/components/ui/menu";
import { Badge, Card, CardHeader, Skeleton } from "@/components/ui/primitives";
import { useInvalidate } from "@/features/queries";
import { formatMs } from "@/features/voice/ai-servers";
import { AiServersCard } from "@/features/voice/ai-servers-section";
import { ConnectionDialog } from "@/features/voice/connection-dialog";
import { SERVICE_LABELS, useCatalog, useConnections, useModelSelection, type Connection, type ModelSelection, type ServiceChoice, type ServiceKind } from "@/features/voice/queries";
import { ServiceEditor } from "@/features/voice/service-editor";
import { PipelineHealthCard, SpeedCard } from "@/features/voice/speed-section";
import { api, errorMessage, unwrap, type Schemas } from "@/lib/api";
import { useHasRole } from "@/lib/session";

const KINDS: ServiceKind[] = ["stt", "llm", "tts", "embeddings"];

export default function AiProvidersPage() {
  const catalog = useCatalog();
  const connections = useConnections();
  const canEdit = useHasRole("admin");
  const [adding, setAdding] = useState(false);

  const error = catalog.error ?? connections.error;
  return (
    <>
      <PageHeader
        title="AI providers"
        description="Choose the models your agents use to hear, think and speak. Use cloud providers with your own keys, or models running on your own servers."
        actions={
          canEdit ? (
            <Button variant="primary" onClick={() => setAdding(true)}>
              <Plus /> Add connection
            </Button>
          ) : null
        }
      />
      {error ? (
        <Card>
          <ErrorState error={error} onRetry={() => void Promise.all([catalog.refetch(), connections.refetch()])} />
        </Card>
      ) : (
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
          <div className="flex min-w-0 flex-col gap-5">
            <ModelsCard canEdit={canEdit} />
            <AiServersCard canEdit={canEdit} />
            <SpeedCard catalog={catalog.data} connections={connections.data} onAddConnection={() => setAdding(true)} />
          </div>
          <div className="flex flex-col gap-5">
            <ConnectionsCard connections={connections.data} loading={connections.isLoading} canEdit={canEdit} onAdd={() => setAdding(true)} />
            <PipelineHealthCard />
            <SelfHostedCard />
          </div>
        </div>
      )}
      <ConnectionDialog open={adding} onOpenChange={setAdding} catalog={catalog.data} />
    </>
  );
}

function ModelsCard({ canEdit }: { canEdit: boolean }) {
  const catalog = useCatalog();
  const connections = useConnections();
  const selection = useModelSelection();
  const invalidate = useInvalidate();
  const [draft, setDraft] = useState<ModelSelection>({});
  const [check, setCheck] = useState<Schemas["ProviderCheck"] | null>(null);

  useEffect(() => {
    if (selection.data) setDraft(selection.data);
  }, [selection.data]);

  const dirty = JSON.stringify(draft) !== JSON.stringify(selection.data ?? {});
  const complete = Boolean(draft.llm && draft.stt && draft.tts);

  const save = useMutation({
    mutationFn: () =>
      unwrap(api.PUT("/api/v1/ai/models", { body: { llm: draft.llm!, stt: draft.stt!, tts: draft.tts!, embeddings: draft.embeddings ?? null } })),
    onSuccess: () => {
      toast.success("Models saved. New calls use them right away.");
      setCheck(null);
      void invalidate("ai");
    },
  });
  const runCheck = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/ai/models/check")),
    onSuccess: setCheck,
    onError: (e) => toast.error(errorMessage(e)),
  });

  return (
    <Card>
      <CardHeader
        title="Models for calls"
        description="Every agent in this organization uses these unless an agent overrides them."
        action={
          selection.data?.llm && selection.data.stt && selection.data.tts ? (
            <Button size="sm" loading={runCheck.isPending} onClick={() => runCheck.mutate()}>
              Test providers
            </Button>
          ) : null
        }
      />
      {check ? (
        <div
          role="status"
          className={`mx-4 mt-4 flex items-start gap-2 rounded-md px-3 py-2 text-[13px] ${check.ok ? "bg-success-soft text-success" : "bg-warn-soft text-warn"}`}
        >
          {check.ok ? <CheckCircle2 className="mt-px size-4 shrink-0" /> : <TriangleAlert className="mt-px size-4 shrink-0" />}
          <div>{check.ok ? "All providers responded. Agents are ready to take calls." : check.problems.map((p) => <p key={p}>{p}</p>)}</div>
        </div>
      ) : null}
      {selection.isLoading || catalog.isLoading ? (
        <div className="flex flex-col gap-3 p-4">
          {Array.from({ length: 3 }, (_, i) => (
            <Skeleton key={i} className="h-20" />
          ))}
        </div>
      ) : (
        <div className="divide-y divide-line">
          {KINDS.map((kind, i) => (
            <section key={kind} className="grid gap-4 px-4 py-5 md:grid-cols-[220px_minmax(0,1fr)]" aria-labelledby={`svc-${kind}`}>
              <div>
                <div className="flex items-center gap-2">
                  <span className="grid size-5 place-items-center rounded bg-surface-2 font-mono text-[10.5px] text-muted">{i + 1}</span>
                  <h3 id={`svc-${kind}`} className="text-[13.5px] font-semibold">
                    {SERVICE_LABELS[kind].title}
                  </h3>
                  {kind === "embeddings" ? <Badge>Optional</Badge> : null}
                </div>
                <p className="mt-1 text-[12.5px] text-muted">{SERVICE_LABELS[kind].description}</p>
              </div>
              <div className="flex min-w-0 flex-col gap-3">
                <ServiceEditor
                  kind={kind}
                  value={(draft[kind] as ServiceChoice | null | undefined) ?? null}
                  onChange={(v) => setDraft({ ...draft, [kind]: v })}
                  catalog={catalog.data}
                  connections={connections.data}
                  disabled={!canEdit}
                  allowNone={kind === "embeddings"}
                />
                {kind === "llm" && selection.data?.llm_fallback ? <FallbackNote fallback={selection.data.llm_fallback} connections={connections.data} /> : null}
              </div>
            </section>
          ))}
        </div>
      )}
      {canEdit ? (
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3">
          <div className="min-w-0 flex-1">
            <FormError message={save.error ? errorMessage(save.error) : null} />
            {!save.error && !complete ? <p className="text-[12.5px] text-muted">Choose speech-to-text, a language model and a voice to enable calls.</p> : null}
          </div>
          <Button variant="primary" disabled={!dirty || !complete} loading={save.isPending} onClick={() => save.mutate()}>
            Save models
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

function FallbackNote({ fallback, connections }: { fallback: NonNullable<ModelSelection["llm_fallback"]>; connections: Connection[] | undefined }) {
  const name = connections?.find((c) => c.id === fallback.connection_id)?.name ?? "another connection";
  const model = fallback.settings?.model ? ` (${String(fallback.settings.model)})` : "";
  const after = fallback.after_ms != null ? `gives no output within ${formatMs(fallback.after_ms)}` : null;
  const when = after && fallback.on_error ? `${after} or fails` : (after ?? "fails");
  return (
    <p className="rounded-md bg-surface-2 px-3 py-2 text-[12.5px] text-ink-2">
      <span className="font-medium">Fallback:</span> if this model {when}, {name}
      {model} answers instead. It is set with Use for this organization under AI servers.
    </p>
  );
}

function ConnectionsCard({ connections, loading, canEdit, onAdd }: { connections: Connection[] | undefined; loading: boolean; canEdit: boolean; onAdd: () => void }) {
  const catalog = useCatalog();
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState<Connection | null>(null);
  const [removing, setRemoving] = useState<Connection | null>(null);
  const remove = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/api/v1/ai/connections/{connection_id}", { params: { path: { connection_id: id } } })),
    onSuccess: () => {
      toast.success("Connection removed");
      setRemoving(null);
      void invalidate("ai");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  return (
    <Card>
      <CardHeader title="Connections" description="Accounts and servers your agents can use" />
      {loading ? (
        <div className="p-4">
          <Skeleton className="h-16" />
        </div>
      ) : !connections?.length ? (
        <EmptyState
          icon={<KeyRound />}
          title="No connections yet"
          description="Add a provider account, such as OpenAI, Deepgram or ElevenLabs, or a self-hosted model server."
          action={canEdit ? <Button onClick={onAdd}>Add connection</Button> : null}
          className="py-10"
        />
      ) : (
        <ul className="divide-y divide-line">
          {connections.map((c) => (
            <li key={c.id} className="flex items-start gap-3 px-4 py-3">
              <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-md bg-surface-2 text-muted">
                {c.provider === "speaches" ? <Server className="size-4" /> : <Cpu className="size-4" />}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-[13px] font-semibold">{c.name}</p>
                <p className="truncate text-[12px] text-muted">{c.provider_title}</p>
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {c.configured_credentials.map((k) => (
                    <Badge key={k} tone="success">
                      {k === "api_key" ? "Key stored" : `${k} stored`}
                    </Badge>
                  ))}
                  {c.connection_settings.base_url ? <Badge className="max-w-full truncate font-mono">{String(c.connection_settings.base_url)}</Badge> : null}
                  {c.hosted_trial ? <Badge tone="warn">Third-party hosted</Badge> : null}
                </div>
              </div>
              {canEdit && !c.hosted_trial ? (
                <MenuRoot>
                  <MenuTrigger asChild>
                    <Button size="icon-sm" variant="ghost" aria-label={`Actions for ${c.name}`}>
                      <MoreHorizontal />
                    </Button>
                  </MenuTrigger>
                  <MenuContent>
                    <MenuItem onSelect={() => setEditing(c)}>Rotate key or edit</MenuItem>
                    <MenuItem tone="danger" onSelect={() => setRemoving(c)}>
                      Remove
                    </MenuItem>
                  </MenuContent>
                </MenuRoot>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {editing ? <ConnectionDialog open onOpenChange={(o) => !o && setEditing(null)} catalog={catalog.data} existing={editing} /> : null}
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={`Remove ${removing?.name ?? "connection"}?`}
        description="Agents can no longer use this provider. Connections in use by the models above can't be removed."
        confirmLabel="Remove connection"
        loading={remove.isPending}
        onConfirm={() => removing && remove.mutate(removing.id)}
      />
    </Card>
  );
}

function SelfHostedCard() {
  return (
    <Card>
      <CardHeader title="Run models on your own servers" description="Keep audio and transcripts inside your network" />
      <div className="flex flex-col gap-3 px-4 py-4 text-[12.5px] text-ink-2">
        <p>
          Add a <strong>Self-hosted models</strong> connection pointing at any OpenAI-compatible server:
        </p>
        <dl className="grid grid-cols-[88px_minmax(0,1fr)] gap-x-3 gap-y-1.5">
          <dt className="text-muted">Language</dt>
          <dd className="font-mono text-[11.5px]">Ollama · vLLM · LM Studio</dd>
          <dt className="text-muted">Speech</dt>
          <dd className="font-mono text-[11.5px]">Whisper (faster-whisper)</dd>
          <dt className="text-muted">Voice</dt>
          <dd className="font-mono text-[11.5px]">Kokoro · Piper</dd>
        </dl>
        <p>
          Running the ATLAS models host on another server? Add it under <strong>AI servers</strong> instead: ATLAS creates these connections for it and keeps its token in one place.
        </p>
        <p className="rounded-md bg-warn-soft px-3 py-2 text-warn">
          Without a GPU, replies take noticeably longer. Use small models (3B parameters or less) for testing, and a GPU server for production calls.
        </p>
      </div>
    </Card>
  );
}
