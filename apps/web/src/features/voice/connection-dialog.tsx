import { useMutation } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Badge, Input } from "@/components/ui/primitives";
import { Select, SelectGroup, SelectItem } from "@/components/ui/select";
import { useInvalidate } from "@/features/queries";
import type { Catalog, Connection, ServiceKind } from "@/features/voice/queries";
import { api, errorMessage, unwrap } from "@/lib/api";

const FIELD_LABELS: Record<string, string> = {
  api_key: "API key",
  credentials: "Service account JSON",
  aws_access_key: "AWS access key ID",
  aws_secret_key: "AWS secret access key",
  aws_session_token: "AWS session token",
  base_url: "Base URL",
  endpoint: "Endpoint",
  project_id: "Project ID",
  location: "Location",
  aws_region: "AWS region",
  region: "Region",
  api_version: "API version",
  group_id: "Group ID",
  bill_to: "Billing account",
};
const SELF_HOSTED_HINT = "http://host.docker.internal:11434/v1 for Ollama, or your Whisper/Kokoro server's /v1 URL";
const KIND_SHORT: Record<ServiceKind, string> = { llm: "LLM", stt: "STT", tts: "Voice", embeddings: "Knowledge" };

interface ProviderEntry {
  id: string;
  title: string;
  selfHosted: boolean;
  kinds: ServiceKind[];
  credentialFields: string[];
  connectionFields: string[];
  credentialRequired: boolean;
}

function providers(catalog: Catalog): ProviderEntry[] {
  const map = new Map<string, ProviderEntry>();
  for (const kind of ["llm", "stt", "tts", "embeddings"] as ServiceKind[]) {
    for (const p of catalog[kind]) {
      const e = map.get(p.id) ?? {
        id: p.id,
        title: p.self_hosted ? "Self-hosted models" : p.title,
        selfHosted: p.self_hosted,
        kinds: [],
        credentialFields: [],
        connectionFields: [],
        credentialRequired: false,
      };
      e.kinds.push(kind);
      e.credentialFields = [...new Set([...e.credentialFields, ...p.credential_fields])];
      e.connectionFields = [...new Set([...e.connectionFields, ...p.connection_fields])];
      e.credentialRequired ||= p.credential_required;
      map.set(p.id, e);
    }
  }
  return [...map.values()].sort((a, b) => Number(a.selfHosted) - Number(b.selfHosted) || a.title.localeCompare(b.title));
}

export function ConnectionDialog({
  open,
  onOpenChange,
  catalog,
  existing,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  catalog: Catalog | undefined;
  existing?: Connection;
}) {
  const invalidate = useInvalidate();
  const list = useMemo(() => (catalog ? providers(catalog) : []), [catalog]);
  const [providerId, setProviderId] = useState(existing?.provider ?? "");
  const [name, setName] = useState(existing?.name ?? "");
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [settings, setSettings] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(existing?.connection_settings ?? {}).map(([k, v]) => [k, String(v ?? "")])),
  );
  const provider = list.find((p) => p.id === providerId);
  const rotating = Boolean(existing);

  const reset = () => {
    setProviderId(existing?.provider ?? "");
    setName(existing?.name ?? "");
    setSecrets({});
    save.reset();
  };

  const save = useMutation({
    mutationFn: () =>
      existing
        ? unwrap(
            api.PATCH("/api/v1/ai/connections/{connection_id}", {
              params: { path: { connection_id: existing.id } },
              body: { name, credentials: secrets, connection_settings: provider?.connectionFields.length ? settings : undefined },
            }),
          )
        : unwrap(api.POST("/api/v1/ai/connections", { body: { name, provider: providerId, credentials: secrets, connection_settings: settings } })),
    onSuccess: (c) => {
      toast.success(existing ? `Updated ${c.name}` : `Connected ${c.name}`);
      void invalidate("ai");
      onOpenChange(false);
      reset();
    },
  });

  const missingSecret =
    !rotating && provider?.credentialRequired && provider.credentialFields.some((f) => f === "api_key" || f === "credentials")
      ? !Object.values(secrets).some((v) => v.trim())
      : false;
  const missingUrl = provider?.selfHosted && !settings.base_url?.trim();

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o);
        if (!o) reset();
      }}
      title={rotating ? `Edit ${existing?.name}` : "Add a connection"}
      description={
        rotating
          ? "Leave secret fields empty to keep the stored values. New keys are verified before they replace the old ones."
          : "Keys are verified with the provider before they are saved, and are never shown again."
      }
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" disabled={!providerId || !name.trim() || missingSecret || missingUrl} loading={save.isPending} onClick={() => save.mutate()}>
            {rotating ? "Save changes" : "Verify and connect"}
          </Button>
        </>
      }
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <FormError message={save.error ? errorMessage(save.error) : null} />
        {!rotating ? (
          <Field label="Provider" required>
            <Select
              value={providerId}
              placeholder="Choose a provider…"
              onValueChange={(v) => {
                setProviderId(v);
                const p = list.find((x) => x.id === v);
                if (p && !name) setName(p.selfHosted ? "Local models" : p.title);
              }}
            >
              <SelectGroup label="Cloud providers">
                {list.filter((p) => !p.selfHosted).map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.title}
                  </SelectItem>
                ))}
              </SelectGroup>
              <SelectGroup label="Run on your own servers">
                {list.filter((p) => p.selfHosted).map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.title}
                  </SelectItem>
                ))}
              </SelectGroup>
            </Select>
          </Field>
        ) : null}
        {provider ? (
          <div className="flex flex-wrap items-center gap-1.5 text-[12px] text-muted">
            Can be used for:
            {provider.kinds.map((k) => (
              <Badge key={k}>{KIND_SHORT[k]}</Badge>
            ))}
          </div>
        ) : null}
        <Field label="Name" hint="How this connection appears when choosing models" required>
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={128} />
        </Field>
        {provider?.connectionFields.map((f) => (
          <Field
            key={f}
            label={FIELD_LABELS[f] ?? f}
            required={provider.selfHosted && f === "base_url"}
            hint={provider.selfHosted && f === "base_url" ? SELF_HOSTED_HINT : f === "base_url" ? "Leave empty to use the provider's standard endpoint" : undefined}
          >
            <Input value={settings[f] ?? ""} onChange={(e) => setSettings({ ...settings, [f]: e.target.value })} className="font-mono" autoComplete="off" />
          </Field>
        ))}
        {provider?.credentialFields.map((f) => (
          <Field key={f} label={FIELD_LABELS[f] ?? f} required={!rotating && provider.credentialRequired && !provider.selfHosted} hint={provider.selfHosted ? "Only if your server requires one" : undefined}>
            <Input
              type="password"
              autoComplete="new-password"
              spellCheck={false}
              value={secrets[f] ?? ""}
              onChange={(e) => setSecrets({ ...secrets, [f]: e.target.value })}
              placeholder={rotating && existing?.configured_credentials.includes(f) ? "•••••••• stored" : undefined}
              className="font-mono"
            />
          </Field>
        ))}
        {provider ? (
          <p className="flex items-start gap-2 rounded-md bg-surface-2 px-3 py-2 text-[12px] text-muted">
            <ShieldCheck className="mt-px size-4 shrink-0 text-success" aria-hidden />
            Secrets are encrypted in the voice service and only used to call this provider. They never appear in logs or in this app again.
          </p>
        ) : null}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}
