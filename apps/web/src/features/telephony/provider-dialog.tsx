import { useMutation } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Badge, Input, Label, Textarea } from "@/components/ui/primitives";
import { Select, SelectGroup, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useInvalidate } from "@/features/queries";
import type { CredentialField, Provider, ProviderOption } from "@/features/telephony/queries";
import { api, errorMessage, unwrap } from "@/lib/api";

function nonEmpty(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim() !== ""));
}

/**
 * Connect or edit a calling provider. The form is built from the catalog's credential
 * fields; secret fields are write-only and show only a "stored" placeholder when editing.
 */
export function ProviderDialog({
  open,
  onOpenChange,
  catalog,
  existing,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  catalog: ProviderOption[] | undefined;
  existing?: Provider;
}) {
  const invalidate = useInvalidate();
  const editing = Boolean(existing);
  const [providerId, setProviderId] = useState(existing?.provider ?? "");
  const [name, setName] = useState(existing?.name ?? "");
  const [values, setValues] = useState<Record<string, string>>({});
  const [defaultOutbound, setDefaultOutbound] = useState(false);
  const option = catalog?.find((p) => p.id === providerId);

  const reset = () => {
    setProviderId(existing?.provider ?? "");
    setName(existing?.name ?? "");
    setValues({});
    setDefaultOutbound(false);
    save.reset();
  };

  const save = useMutation({
    mutationFn: () => {
      const credentials = nonEmpty(values);
      if (existing) {
        return unwrap(
          api.PATCH("/api/v1/telephony/providers/{provider_id}", {
            params: { path: { provider_id: existing.id } },
            body: { name: name.trim() !== existing.name ? name.trim() : null, credentials: Object.keys(credentials).length ? credentials : null },
          }),
        );
      }
      return unwrap(api.POST("/api/v1/telephony/providers", { body: { name: name.trim(), provider: providerId, credentials, is_default_outbound: defaultOutbound } }));
    },
    onSuccess: (p) => {
      toast.success(existing ? `Updated ${p.name}` : `Connected ${p.name}`);
      void invalidate("telephony");
      onOpenChange(false);
      reset();
    },
  });

  const missingRequired = !editing && (option?.fields ?? []).some((f) => f.required && !(values[f.name] ?? "").trim());
  const canSubmit = Boolean(option) && name.trim() !== "" && !missingRequired;

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o);
        if (!o) reset();
      }}
      title={editing ? `Edit ${existing?.name}` : "Connect a calling provider"}
      description={
        editing
          ? "Leave a field empty to keep what is on file. The voice service checks that the result is a complete, valid credential set."
          : "Agents answer and place calls over an account you own. Credentials go straight to the voice service and are never shown again."
      }
      width="max-w-xl"
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" disabled={!canSubmit} loading={save.isPending} onClick={() => save.mutate()}>
            {editing ? "Save changes" : "Connect"}
          </Button>
        </>
      }
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) save.mutate();
        }}
      >
        <FormError message={save.error ? errorMessage(save.error) : null} />
        {!editing ? (
          <Field label="Provider" required hint={option?.connectivity === "sip" ? "Bring your own Asterisk connected to any SIP trunk. No provider webhooks are involved." : undefined}>
            <Select
              value={providerId}
              placeholder="Choose a provider…"
              onValueChange={(v) => {
                setProviderId(v);
                setValues({});
                const p = catalog?.find((x) => x.id === v);
                if (p && !name.trim()) setName(p.title);
              }}
            >
              <SelectGroup label="Cloud providers">
                {(catalog ?? [])
                  .filter((p) => p.connectivity === "api")
                  .map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.title}
                    </SelectItem>
                  ))}
              </SelectGroup>
              <SelectGroup label="Your own SIP trunk">
                {(catalog ?? [])
                  .filter((p) => p.connectivity === "sip")
                  .map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.title}
                    </SelectItem>
                  ))}
              </SelectGroup>
            </Select>
          </Field>
        ) : (
          <div className="flex items-center gap-2 text-[12.5px] text-muted">
            <Badge>{existing?.provider_title}</Badge>
            {existing?.connectivity === "sip" ? <Badge>SIP trunk</Badge> : null}
          </div>
        )}
        <Field label="Name" required hint="How this account appears across ATLAS, for example “Main Twilio account”.">
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} autoComplete="off" />
        </Field>

        {option ? (
          <fieldset className="flex flex-col gap-4 rounded-lg border border-line p-4">
            <legend className="px-1 text-[11px] font-semibold uppercase tracking-wider text-faint">Credentials</legend>
            {option.fields.map((f) => (
              <CredentialInput
                key={f.name}
                field={f}
                value={values[f.name] ?? ""}
                onChange={(v) => setValues({ ...values, [f.name]: v })}
                editing={editing}
                stored={existing?.configured_fields.includes(f.name) ?? false}
              />
            ))}
          </fieldset>
        ) : null}

        {!editing && option?.supports_outbound ? (
          <div className="flex items-start justify-between gap-4 rounded-lg border border-line px-4 py-3">
            <div>
              <Label htmlFor="default-outbound">Use for outbound calls</Label>
              <p className="mt-0.5 text-[12px] text-muted">Campaigns and call-now use this account unless an agent says otherwise.</p>
            </div>
            <Switch id="default-outbound" checked={defaultOutbound} onCheckedChange={setDefaultOutbound} />
          </div>
        ) : null}

        {option ? (
          <p className="flex items-start gap-2 rounded-md bg-surface-2 px-3 py-2 text-[12px] text-muted">
            <ShieldCheck className="mt-px size-4 shrink-0 text-success" aria-hidden />
            Stored encrypted by the voice service and used only to talk to {option.title}. ATLAS records which fields are on file and every change in the audit log, never the values.
          </p>
        ) : null}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

function CredentialInput({
  field,
  value,
  onChange,
  editing,
  stored,
}: {
  field: CredentialField;
  value: string;
  onChange: (value: string) => void;
  editing: boolean;
  stored: boolean;
}) {
  const required = !editing && field.required;
  const hint = [field.help, editing && stored ? "On file — leave empty to keep it." : !field.required ? "Optional." : null].filter(Boolean).join(" ");
  const placeholder = editing && stored ? (field.secret ? "•••••••• stored" : "Stored") : undefined;
  const common = {
    value,
    placeholder,
    autoComplete: field.secret ? "new-password" : "off",
    spellCheck: false,
    className: "font-mono",
  } as const;
  return (
    <Field label={field.label} required={required} hint={hint || undefined}>
      {field.multiline ? (
        <Textarea {...common} rows={5} onChange={(e) => onChange(e.target.value)} className="min-h-24 font-mono text-[12px]" />
      ) : (
        <Input {...common} type={field.secret ? "password" : "text"} onChange={(e) => onChange(e.target.value)} />
      )}
    </Field>
  );
}
