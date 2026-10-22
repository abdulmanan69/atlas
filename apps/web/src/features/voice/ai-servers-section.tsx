/**
 * AI servers on the AI providers screen: self-hosted models hosts registered once with
 * their AI gateway's address and token. ATLAS adds their models as connections, checks
 * them every two minutes, and keeps the token in one place.
 */
import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, MinusCircle, MoreHorizontal, Plus, RefreshCw, Server, ShieldCheck } from "lucide-react";
import { useState, type ChangeEvent } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { ConfirmDialog, Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { MenuContent, MenuItem, MenuRoot, MenuTrigger } from "@/components/ui/menu";
import { Card, CardHeader, Checkbox, Input, Skeleton } from "@/components/ui/primitives";
import { Select, SelectItem } from "@/components/ui/select";
import { useInvalidate } from "@/features/queries";
import {
  DEFAULT_FALLBACK_MS,
  FALLBACK_DELAYS_MS,
  fieldError,
  formatMs,
  formError,
  SERVER_STATUS,
  SERVICE_STATE,
  serviceOf,
  serviceSummary,
  useAiServers,
  type AiServer,
  type AiServerRegistered,
  type AiServerService,
} from "@/features/voice/ai-servers";
import { connectionsFor, useCatalog, useConnections, useModelSelection } from "@/features/voice/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

const DEEPGRAM_WHY =
  "The voice service checks every Deepgram-type key with Deepgram's cloud when it saves the connection, so a real key is needed once; no audio goes to Deepgram.";
const SECRET_HINT = "The part after /live-stt/ in the live speech-to-text address shown when the models host was deployed.";

export function AiServersCard({ canEdit }: { canEdit: boolean }) {
  const servers = useAiServers();
  const [adding, setAdding] = useState(false);
  const [using, setUsing] = useState<AiServer | null>(null);
  const [replacing, setReplacing] = useState<AiServer | null>(null);
  const [removing, setRemoving] = useState<AiServer | null>(null);

  return (
    <Card>
      <CardHeader
        title="AI servers"
        description="Your own models hosts, reached through their AI gateway. ATLAS adds their models as connections and checks them every two minutes."
        action={
          canEdit ? (
            <Button size="sm" onClick={() => setAdding(true)}>
              <Plus /> Add AI server
            </Button>
          ) : null
        }
      />
      {servers.isLoading ? (
        <div className="p-4">
          <Skeleton className="h-28" />
        </div>
      ) : servers.error ? (
        <ErrorState error={servers.error} onRetry={() => void servers.refetch()} />
      ) : !servers.data?.length ? (
        <EmptyState
          icon={<Server />}
          title="No AI servers yet"
          description="Register a models host once with its address and token. ATLAS checks it and adds its language model, Whisper, Pocket TTS, expressive voice and live speech-to-text as connections."
          action={canEdit ? <Button onClick={() => setAdding(true)}>Add AI server</Button> : null}
          className="py-10"
        />
      ) : (
        <ul className="divide-y divide-line">
          {servers.data.map((s) => (
            <ServerItem
              key={s.id}
              server={s}
              canEdit={canEdit}
              onUse={() => setUsing(s)}
              onReplace={() => setReplacing(s)}
              onRemove={() => setRemoving(s)}
            />
          ))}
        </ul>
      )}
      <AddAiServerDialog open={adding} onOpenChange={setAdding} onUse={setUsing} />
      {using ? <UseServerDialog server={using} onClose={() => setUsing(null)} /> : null}
      {replacing ? <ReplaceTokenDialog server={replacing} onClose={() => setReplacing(null)} /> : null}
      <RemoveServerDialog server={removing} onClose={() => setRemoving(null)} />
    </Card>
  );
}

function ServerItem({
  server,
  canEdit,
  onUse,
  onReplace,
  onRemove,
}: {
  server: AiServer;
  canEdit: boolean;
  onUse: () => void;
  onReplace: () => void;
  onRemove: () => void;
}) {
  const invalidate = useInvalidate();
  const check = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/ai/servers/{server_id}/check", { params: { path: { server_id: server.id } } })),
    onSuccess: (s) => {
      void invalidate("ai");
      if (s.status === "down") toast.error(`${s.name} is down. ${s.last_error ?? ""}`.trim());
      else toast.success(`Checked ${s.name}: ${SERVER_STATUS[s.status].label.toLowerCase()}.`);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const status = SERVER_STATUS[server.status];

  return (
    <li className="px-4 py-4">
      <div className="flex flex-wrap items-start gap-3">
        <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-md bg-surface-2 text-muted">
          <Server className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13.5px] font-semibold">{server.name}</p>
          <p className="truncate font-mono text-[11.5px] text-muted">{server.url}</p>
          <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px]">
            <span className="inline-flex items-center gap-1.5 font-medium text-ink-2">
              <span aria-hidden className={cn("size-2 rounded-full", status.dot)} />
              {status.label}
            </span>
            <span className="text-muted">{server.last_checked_at ? `Checked ${formatRelative(server.last_checked_at)}` : "Not checked yet"}</span>
            {server.last_latency_ms != null ? <span className="text-muted">{server.last_latency_ms} ms round trip</span> : null}
          </p>
        </div>
        {canEdit ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <Button size="sm" loading={check.isPending} onClick={() => check.mutate()}>
              {check.isPending ? null : <RefreshCw />} Check now
            </Button>
            <Button size="sm" variant="primary" onClick={onUse}>
              Use for this organization
            </Button>
            <MenuRoot>
              <MenuTrigger asChild>
                <Button size="icon-sm" variant="ghost" aria-label={`More actions for ${server.name}`}>
                  <MoreHorizontal />
                </Button>
              </MenuTrigger>
              <MenuContent>
                <MenuItem onSelect={onReplace}>Replace token</MenuItem>
                <MenuItem tone="danger" onSelect={onRemove}>
                  Remove
                </MenuItem>
              </MenuContent>
            </MenuRoot>
          </div>
        ) : null}
      </div>
      {server.last_error ? (
        <p role="status" className="mt-3 rounded-md bg-danger-soft px-3 py-2 text-[12.5px] text-danger">
          {server.last_error}
        </p>
      ) : null}
      <ul className="mt-3 grid gap-2 sm:grid-cols-2" aria-label={`Services of ${server.name}`}>
        {server.services.map((s) => (
          <ServiceRow key={s.service} service={s} />
        ))}
      </ul>
    </li>
  );
}

function missingConnection(s: AiServerService): string | null {
  if (s.state === "not_offered" || s.connection_id) return null;
  if (s.service === "live_stt") return "Not set up: needs the live speech-to-text secret and a Deepgram key (Replace token).";
  return "No connection yet: Check now adds it once the service is up.";
}

function ServiceRow({ service: s }: { service: AiServerService }) {
  const state = SERVICE_STATE[s.state];
  const label = s.state === "down" && s.error ? `${state.label} (${s.error})` : state.label;
  const missing = missingConnection(s);
  return (
    <li className={cn("flex items-start gap-2 rounded-md border border-line px-3 py-2", s.state === "not_offered" && "bg-surface-2/60")}>
      <span aria-hidden className={cn("mt-1.5 size-2 shrink-0 rounded-full", state.dot)} />
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-baseline gap-x-2 text-[12.5px]">
          <span className="font-semibold text-ink">{s.title}</span>
          <span className="text-muted">
            {label}
            {s.state === "up" && s.latency_ms != null ? ` · ${s.latency_ms} ms` : ""}
          </span>
        </p>
        <p className="break-words font-mono text-[11.5px] text-muted">{serviceSummary(s)}</p>
        {missing ? <p className="mt-0.5 text-[11.5px] text-warn">{missing}</p> : null}
      </div>
    </li>
  );
}

// ------------------------------------------------------------------ add

const ADD_FIELDS = ["name", "url", "token", "live_stt_secret", "deepgram_key"] as const;
const EMPTY_ADD = { name: "", url: "", token: "", secret: "", key: "" };

export function AddAiServerDialog({
  open,
  onOpenChange,
  onUse,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onUse: (server: AiServer) => void;
}) {
  const invalidate = useInvalidate();
  const [form, setForm] = useState(EMPTY_ADD);
  const [result, setResult] = useState<AiServerRegistered | null>(null);
  const add = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/ai/servers", {
          body: {
            name: form.name.trim(),
            url: form.url.trim(),
            token: form.token.trim(),
            live_stt_secret: form.secret.trim() || null,
            deepgram_key: form.key.trim() || null,
          },
        }),
      ),
    onSuccess: (server) => {
      setResult(server);
      setForm(EMPTY_ADD);
      void invalidate("ai");
    },
  });
  const close = () => {
    onOpenChange(false);
    setForm(EMPTY_ADD);
    setResult(null);
    add.reset();
  };
  const bind = (k: keyof typeof EMPTY_ADD) => ({
    value: form[k],
    onChange: (e: ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value }),
  });
  const secret = { type: "password", autoComplete: "new-password", spellCheck: false, className: "font-mono" } as const;
  const err = (f: (typeof ADD_FIELDS)[number]) => fieldError(add.error, f);

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => (o ? onOpenChange(true) : close())}
      title={result ? `Added ${result.name}` : "Add an AI server"}
      description={
        result
          ? "What the server offered, and the connections ATLAS created for it."
          : "A models host running the AI gateway. ATLAS checks it with the token first, then adds its models as connections."
      }
      footer={
        result ? (
          <>
            <Button onClick={close}>Done</Button>
            <Button
              variant="primary"
              onClick={() => {
                close();
                onUse(result);
              }}
            >
              Use for this organization
            </Button>
          </>
        ) : (
          <>
            <Button onClick={close}>Cancel</Button>
            <Button variant="primary" disabled={!form.name.trim() || !form.url.trim() || !form.token.trim()} loading={add.isPending} onClick={() => add.mutate()}>
              Check and add
            </Button>
          </>
        )
      }
    >
      {result ? (
        <ul className="flex flex-col gap-3" aria-label="Connections">
          {result.setup.map((s) => (
            <li key={s.service} className="flex items-start gap-2">
              {s.created ? <CheckCircle2 className="mt-px size-4 shrink-0 text-success" aria-hidden /> : <MinusCircle className="mt-px size-4 shrink-0 text-muted" aria-hidden />}
              <div className="min-w-0">
                <p className="text-[13px] font-medium">{serviceOf(result, s.service)?.title ?? s.service}</p>
                <p className="text-[12.5px] text-muted">{s.created ? `Added as ${s.connection_name}` : `Not added. ${s.reason ?? ""}`.trim()}</p>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (form.name.trim() && form.url.trim() && form.token.trim()) add.mutate();
          }}
        >
          <FormError message={formError(add.error, ADD_FIELDS)} />
          <Field label="Name" hint="How this server appears in ATLAS" error={err("name")} required>
            <Input {...bind("name")} placeholder="GPU server" maxLength={60} />
          </Field>
          <Field label="Address" hint="The AI gateway's address, shown at the end of the models host's deploy" error={err("url")} required>
            <Input {...bind("url")} placeholder="https://ai.203-0-113-7.sslip.io" className="font-mono" autoComplete="off" spellCheck={false} />
          </Field>
          <Field label="Token" hint="Shown once when the models host was deployed (ATLAS_AI_TOKEN). Stored encrypted and never shown again." error={err("token")} required>
            <Input {...bind("token")} {...secret} />
          </Field>
          <div className="flex flex-col gap-3 rounded-md border border-line p-3">
            <div>
              <p className="text-[13px] font-medium text-ink-2">Live speech-to-text (optional)</p>
              <p className="mt-0.5 text-[12px] text-muted">Recognises the caller's words while they speak. Only if the server offers it.</p>
            </div>
            <Field label="Live speech-to-text secret" hint={SECRET_HINT} error={err("live_stt_secret")}>
              <Input {...bind("secret")} {...secret} />
            </Field>
            <Field label="Deepgram API key" hint={DEEPGRAM_WHY} error={err("deepgram_key")}>
              <Input {...bind("key")} {...secret} />
            </Field>
          </div>
          <p className="flex items-start gap-2 rounded-md bg-surface-2 px-3 py-2 text-[12px] text-muted">
            <ShieldCheck className="mt-px size-4 shrink-0 text-success" aria-hidden />
            The token and secrets are only sent to the server and the voice service. They never appear in logs, the audit log or this app again.
          </p>
          <button type="submit" hidden />
        </form>
      )}
    </Dialog>
  );
}

// ------------------------------------------------------------------ use

function UseServerDialog({ server, onClose }: { server: AiServer; onClose: () => void }) {
  const invalidate = useInvalidate();
  const catalog = useCatalog();
  const connections = useConnections();
  const selection = useModelSelection();
  const llm = serviceOf(server, "llm");
  const models = llm?.models ?? [];
  const liveAvailable = Boolean(serviceOf(server, "live_stt")?.connection_id);
  const sttAvailable = liveAvailable || Boolean(serviceOf(server, "whisper")?.connection_id);
  const expressiveAvailable = Boolean(serviceOf(server, "voice")?.connection_id);
  const pocketAvailable = Boolean(serviceOf(server, "tts")?.connection_id);
  const missing = [
    llm?.connection_id ? null : "language model",
    sttAvailable ? null : "speech-to-text",
    expressiveAvailable || pocketAvailable ? null : "a voice",
  ].filter(Boolean);
  const own = new Set(server.services.map((s) => s.connection_id).filter(Boolean));
  const fallbacks = connectionsFor("llm", catalog.data, connections.data).filter((c) => !own.has(c.id) && !c.hosted_trial);

  const [model, setModel] = useState(models[0] ?? "");
  const [useLive, setUseLive] = useState(liveAvailable);
  const [voice, setVoice] = useState<"expressive" | "pocket">(expressiveAvailable ? "expressive" : "pocket");
  const [fallbackId, setFallbackId] = useState("");
  const [fallbackModel, setFallbackModel] = useState("");
  const [delay, setDelay] = useState<number>(DEFAULT_FALLBACK_MS);
  const current = selection.data?.llm;
  const keepsModel = current && current.connection_id === fallbackId && current.settings?.model ? String(current.settings.model) : null;

  const use = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/ai/servers/{server_id}/use", {
          params: { path: { server_id: server.id } },
          body: {
            llm_model: model.trim() || null,
            use_live_stt: liveAvailable && useLive,
            voice: expressiveAvailable && pocketAvailable ? voice : null,
            fallback_llm_connection_id: fallbackId || null,
            fallback_llm_model: fallbackId ? fallbackModel.trim() || null : null,
            fallback_after_ms: delay,
          },
        }),
      ),
    onSuccess: () => {
      toast.success(`New calls use ${server.name}.`);
      void invalidate("ai");
      onClose();
    },
  });
  const fields = ["llm_model", "fallback_llm_connection_id", "fallback_llm_model"] as const;

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`Use ${server.name} for this organization`}
      description="New calls use this server's language model, speech-to-text and voice. Agents with their own models keep them."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={missing.length > 0 || !model.trim()} loading={use.isPending} onClick={() => use.mutate()}>
            Use {server.name}
          </Button>
        </>
      }
    >
      {missing.length ? (
        <p role="alert" className="rounded-md bg-warn-soft px-3 py-2 text-[13px] text-warn">
          This server has no connection for: {missing.join(", ")}. Check the server: ATLAS adds a connection for each service that is up.
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          <FormError message={formError(use.error, fields)} />
          <Field
            label="Language model"
            hint={llm?.loaded?.length ? `In memory now: ${llm.loaded.join(", ")}. Another model loads on its first request.` : undefined}
            error={fieldError(use.error, "llm_model")}
            required
          >
            {models.length ? (
              <Select value={model} onValueChange={setModel}>
                {models.map((m) => (
                  <SelectItem key={m} value={m}>
                    {m}
                  </SelectItem>
                ))}
              </Select>
            ) : (
              <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder="Model name, e.g. qwen2.5:1.5b" className="font-mono" autoComplete="off" />
            )}
          </Field>
          {liveAvailable ? (
            <div className="flex flex-col gap-1">
              <label className="flex items-start gap-2 text-[13px] font-medium text-ink-2">
                <Checkbox aria-label="Use live speech-to-text" checked={useLive} onCheckedChange={setUseLive} className="mt-0.5" />
                <span>Use live speech-to-text</span>
              </label>
              <p className="pl-6 text-[12px] text-muted">Recognises the caller's words while they speak. Off: Whisper, which transcribes after the caller stops.</p>
            </div>
          ) : null}
          {expressiveAvailable && pocketAvailable ? (
            <Field label="Voice" hint="The expressive voice laughs, sighs and gasps where it fits; Pocket TTS is lighter and runs on a CPU.">
              <Select value={voice} onValueChange={(v) => setVoice(v as "expressive" | "pocket")}>
                <SelectItem value="expressive">Expressive voice</SelectItem>
                <SelectItem value="pocket">Pocket TTS</SelectItem>
              </Select>
            </Field>
          ) : null}
          <Field label="Fallback language model" hint="Answers a turn when this server gives no output in time or fails. Agents with their own language model do not use it." error={fieldError(use.error, "fallback_llm_connection_id")}>
            <Select value={fallbackId} onValueChange={setFallbackId} allowEmpty emptyLabel="None">
              {fallbacks.map((c) => (
                <SelectItem key={c.id} value={c.id} description={c.provider_title}>
                  {c.name}
                </SelectItem>
              ))}
            </Select>
          </Field>
          {fallbackId ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Switch after" hint="Without output from this server">
                <Select value={String(delay)} onValueChange={(v) => setDelay(Number(v))}>
                  {FALLBACK_DELAYS_MS.map((ms) => (
                    <SelectItem key={ms} value={String(ms)}>
                      {formatMs(ms)}
                    </SelectItem>
                  ))}
                </Select>
              </Field>
              <Field label="Fallback model" error={fieldError(use.error, "fallback_llm_model")}>
                <Input
                  value={fallbackModel}
                  onChange={(e) => setFallbackModel(e.target.value)}
                  placeholder={keepsModel ? `Same as now: ${keepsModel}` : "The provider's default"}
                  className="font-mono"
                  autoComplete="off"
                />
              </Field>
            </div>
          ) : null}
          <div className="flex flex-col gap-1">
            <p className="text-[13px] font-medium text-ink-2">Voice</p>
            <p className="text-[12.5px] text-muted">Pocket TTS, with your current voice when this server has it; otherwise its default voice. Change it under Models for calls.</p>
          </div>
        </div>
      )}
    </Dialog>
  );
}

// ------------------------------------------------------------------ replace token

function ReplaceTokenDialog({ server, onClose }: { server: AiServer; onClose: () => void }) {
  const invalidate = useInvalidate();
  const [token, setToken] = useState("");
  const [secret, setSecret] = useState("");
  const [key, setKey] = useState("");
  const live = serviceOf(server, "live_stt");
  const offersLive = Boolean(live && (live.state !== "not_offered" || live.connection_id));
  const any = Boolean(token.trim() || secret.trim() || key.trim());
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/ai/servers/{server_id}", {
          params: { path: { server_id: server.id } },
          body: { token: token.trim() || null, live_stt_secret: secret.trim() || null, deepgram_key: key.trim() || null },
        }),
      ),
    onSuccess: (s) => {
      toast.success(token.trim() ? `${s.name} uses the new token.` : `Saved ${s.name}.`);
      void invalidate("ai");
      onClose();
    },
  });
  const fields = ["token", "live_stt_secret", "deepgram_key"] as const;
  const secretInput = { type: "password", autoComplete: "new-password", spellCheck: false, className: "font-mono" } as const;

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`Replace the token of ${server.name}`}
      description="ATLAS checks the new token with the server before anything changes; then every connection of this server uses it."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!any} loading={save.isPending} onClick={() => save.mutate()}>
            Check and save
          </Button>
        </>
      }
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (any) save.mutate();
        }}
      >
        <FormError message={formError(save.error, fields)} />
        <Field label="New token" hint="ATLAS_AI_TOKEN_NEXT, or the new ATLAS_AI_TOKEN, from the models host" error={fieldError(save.error, "token")}>
          <Input value={token} onChange={(e) => setToken(e.target.value)} {...secretInput} />
        </Field>
        {offersLive ? (
          <div className="flex flex-col gap-3 rounded-md border border-line p-3">
            <div>
              <p className="text-[13px] font-medium text-ink-2">Live speech-to-text</p>
              <p className="mt-0.5 text-[12px] text-muted">
                {live?.connection_id ? "Leave empty to keep the stored secret and key." : "Give both to set up live speech-to-text for this server."}
              </p>
            </div>
            <Field label="Live speech-to-text secret" hint={SECRET_HINT} error={fieldError(save.error, "live_stt_secret")}>
              <Input value={secret} onChange={(e) => setSecret(e.target.value)} placeholder={server.live_stt_secret_set ? "•••••••• stored" : undefined} {...secretInput} />
            </Field>
            <Field label="Deepgram API key" hint={DEEPGRAM_WHY} error={fieldError(save.error, "deepgram_key")}>
              <Input value={key} onChange={(e) => setKey(e.target.value)} {...secretInput} />
            </Field>
          </div>
        ) : null}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ remove

function RemoveServerDialog({ server, onClose }: { server: AiServer | null; onClose: () => void }) {
  const invalidate = useInvalidate();
  const remove = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/api/v1/ai/servers/{server_id}", { params: { path: { server_id: id } } })),
    onSuccess: () => {
      toast.success(`Removed ${server?.name ?? "the AI server"}`);
      void invalidate("ai");
      onClose();
      remove.reset();
    },
  });
  return (
    <ConfirmDialog
      open={server !== null}
      onOpenChange={(o) => {
        if (!o) {
          onClose();
          remove.reset();
        }
      }}
      title={`Remove ${server?.name ?? "AI server"}?`}
      description={
        <>
          Its connections are removed too; voices cloned on it stay on the server. A server the organization's models or an agent use can't be removed.
          {remove.error ? (
            <span role="alert" className="mt-2 block text-danger">
              {errorMessage(remove.error)}
            </span>
          ) : null}
        </>
      }
      confirmLabel="Remove server"
      loading={remove.isPending}
      onConfirm={() => server && remove.mutate(server.id)}
    />
  );
}
