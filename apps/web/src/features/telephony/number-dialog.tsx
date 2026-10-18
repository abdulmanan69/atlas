import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Input } from "@/components/ui/primitives";
import { Select, SelectItem } from "@/components/ui/select";
import { useInvalidate } from "@/features/queries";
import type { Provider } from "@/features/telephony/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { useCurrentMembership } from "@/lib/session";

const REGIONS = ["US", "CA", "GB", "IE", "AU", "NZ", "DE", "FR", "ES", "IT", "NL", "BE", "CH", "AT", "SE", "NO", "DK", "PL", "PT", "IN", "PK", "AE", "SA", "SG", "ZA", "BR", "MX"];
const regionNames = (() => {
  try {
    return new Intl.DisplayNames(undefined, { type: "region" });
  } catch {
    return null;
  }
})();
const regionLabel = (code: string) => {
  try {
    return regionNames?.of(code) ?? code;
  } catch {
    return code;
  }
};

/** Add a number the provider account already owns. The voice service confirms ownership before it is saved. */
export function AddNumberDialog({
  open,
  onOpenChange,
  providers,
  initialProviderId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  providers: Provider[];
  initialProviderId?: string;
}) {
  const invalidate = useInvalidate();
  const orgRegion = useCurrentMembership()?.organization.default_country?.toUpperCase();
  const [providerId, setProviderId] = useState(initialProviderId ?? providers[0]?.id ?? "");
  const [phone, setPhone] = useState("");
  const [label, setLabel] = useState("");
  const [region, setRegion] = useState(orgRegion && REGIONS.includes(orgRegion) ? orgRegion : "US");
  const effectiveProvider = providers.find((p) => p.id === providerId) ?? providers[0];

  const reset = () => {
    setPhone("");
    setLabel("");
    add.reset();
  };
  const add = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/telephony/providers/{provider_id}/numbers", {
          params: { path: { provider_id: effectiveProvider!.id } },
          body: { phone_number: phone.trim(), label: label.trim() || null, default_region: region },
        }),
      ),
    onSuccess: (n) => {
      toast.success(`Added ${n.display}. Assign an agent so it gets answered.`);
      void invalidate("telephony");
      onOpenChange(false);
      reset();
    },
  });
  const canSubmit = Boolean(effectiveProvider) && phone.trim().length >= 3;

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o);
        if (!o) reset();
      }}
      title="Add a phone number"
      description="The number must already belong to the provider account. Any common format works; it is stored as +E.164."
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" disabled={!canSubmit} loading={add.isPending} onClick={() => add.mutate()}>
            Add number
          </Button>
        </>
      }
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) add.mutate();
        }}
      >
        <FormError message={add.error ? errorMessage(add.error) : null} />
        {providers.length > 1 ? (
          <Field label="Provider" required>
            <Select value={providerId} onValueChange={setProviderId}>
              {providers.map((p) => (
                <SelectItem key={p.id} value={p.id} description={p.provider_title}>
                  {p.name}
                </SelectItem>
              ))}
            </Select>
          </Field>
        ) : null}
        <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_160px]">
          <Field label="Phone number" required hint="For example +1 415 555 0100">
            <Input value={phone} onChange={(e) => setPhone(e.target.value)} className="font-mono" inputMode="tel" autoComplete="off" autoFocus maxLength={32} />
          </Field>
          <Field label="Region" hint="Only used when the number is typed without a country code">
            <Select value={region} onValueChange={setRegion} aria-label="Region">
              {REGIONS.map((r) => (
                <SelectItem key={r} value={r} textValue={regionLabel(r)}>
                  <span className="font-mono text-[12px] text-muted">{r}</span> {regionLabel(r)}
                </SelectItem>
              ))}
            </Select>
          </Field>
        </div>
        <Field label="Label" hint="Optional, for example “Sales line” or “Leeds office”">
          <Input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={64} />
        </Field>
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}
