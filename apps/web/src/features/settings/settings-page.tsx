import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Monitor, Moon, Sun } from "lucide-react";
import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { ErrorState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { FormCombobox } from "@/components/ui/combobox";
import { Field, FormError } from "@/components/ui/form";
import { Badge, Card, CardHeader, Input, Skeleton } from "@/components/ui/primitives";
import { qk, useOrgId } from "@/features/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { session, useHasRole } from "@/lib/session";
import { useTheme, type ThemePref } from "@/lib/theme";
import { cn, TIMEZONE_OPTIONS } from "@/lib/utils";

export default function SettingsPage() {
  return (
    <>
      <PageHeader title="Settings" description="Organization profile, appearance and your sign-in sessions." />
      <div className="grid max-w-3xl gap-5">
        <OrganizationCard />
        <AppearanceCard />
        <SessionsCard />
      </div>
    </>
  );
}

const orgSchema = z.object({
  name: z.string().trim().min(2, "Use at least 2 characters").max(200),
  timezone: z.string().min(1),
  default_country: z.string().regex(/^[A-Z]{2}$/, "Two-letter country code"),
});

function OrganizationCard() {
  const orgId = useOrgId();
  const canEdit = useHasRole("owner");
  const qc = useQueryClient();
  const org = useQuery({ queryKey: qk.organization(orgId), queryFn: () => unwrap(api.GET("/api/v1/organization")) });
  const form = useForm<z.infer<typeof orgSchema>>({ resolver: zodResolver(orgSchema) });
  useEffect(() => {
    if (org.data) form.reset({ name: org.data.name, timezone: org.data.timezone, default_country: org.data.default_country });
  }, [org.data, form]);

  const save = useMutation({
    mutationFn: (body: z.infer<typeof orgSchema>) => unwrap(api.PATCH("/api/v1/organization", { body })),
    onSuccess: async (updated) => {
      toast.success("Organization saved");
      await qc.invalidateQueries({ queryKey: qk.organization(orgId) });
      const me = await unwrap(api.GET("/api/v1/auth/me"));
      session.getState().setMemberships(me.memberships);
      form.reset({ name: updated.name, timezone: updated.timezone, default_country: updated.default_country });
    },
  });

  return (
    <Card>
      <CardHeader title="Organization" description={canEdit ? undefined : "Only owners can change these settings."} />
      {org.isError ? (
        <ErrorState error={org.error} />
      ) : !org.data ? (
        <div className="p-4">
          <Skeleton className="h-32" />
        </div>
      ) : (
        <form className="grid gap-4 p-4 sm:grid-cols-2" noValidate onSubmit={form.handleSubmit((v) => save.mutate(v))}>
          <div className="sm:col-span-2">
            <FormError message={save.error ? errorMessage(save.error) : null} />
          </div>
          <Field label="Name" className="sm:col-span-2" error={form.formState.errors.name?.message}>
            <Input disabled={!canEdit} {...form.register("name")} />
          </Field>
          <Field label="Time zone" hint="Calling hours and daily reports use this zone">
            <FormCombobox control={form.control} name="timezone" options={TIMEZONE_OPTIONS} disabled={!canEdit} searchPlaceholder="Search time zones" />
          </Field>
          <Field label="Default country" hint="Applied to phone numbers without a country code" error={form.formState.errors.default_country?.message}>
            <Input disabled={!canEdit} maxLength={2} className="uppercase" {...form.register("default_country", { setValueAs: (v: string) => v.toUpperCase() })} />
          </Field>
          {canEdit ? (
            <div className="flex justify-end sm:col-span-2">
              <Button type="submit" variant="primary" disabled={!form.formState.isDirty} loading={save.isPending}>
                Save changes
              </Button>
            </div>
          ) : null}
        </form>
      )}
    </Card>
  );
}

function AppearanceCard() {
  const [pref, setPref] = useTheme();
  const options: { value: ThemePref; label: string; icon: typeof Sun }[] = [
    { value: "light", label: "Light", icon: Sun },
    { value: "dark", label: "Dark", icon: Moon },
    { value: "system", label: "Match system", icon: Monitor },
  ];
  return (
    <Card>
      <CardHeader title="Appearance" description="Saved on this device." />
      <div role="radiogroup" aria-label="Theme" className="grid grid-cols-3 gap-3 p-4">
        {options.map((o) => (
          <button
            key={o.value}
            role="radio"
            aria-checked={pref === o.value}
            onClick={() => setPref(o.value)}
            className={cn(
              "flex items-center gap-2 rounded-lg border border-line px-3 py-2.5 text-[13px] font-medium text-ink-2 transition-colors hover:border-line-strong",
              pref === o.value && "border-accent bg-accent-soft text-accent-soft-ink",
            )}
          >
            <o.icon className="size-4" /> {o.label}
          </button>
        ))}
      </div>
    </Card>
  );
}

function SessionsCard() {
  const qc = useQueryClient();
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: () => unwrap(api.GET("/api/v1/auth/sessions")) });
  const revoke = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/api/v1/auth/sessions/{session_id}", { params: { path: { session_id: id } } })),
    onSuccess: () => {
      toast.success("Session signed out");
      void qc.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const describe = (ua: string | null) => {
    if (!ua) return "Unknown device";
    const browser = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "Browser";
    const os = /Windows/.test(ua) ? "Windows" : /Mac OS/.test(ua) ? "macOS" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Linux/.test(ua) ? "Linux" : "";
    return os ? `${browser} on ${os}` : browser;
  };

  return (
    <Card>
      <CardHeader title="Your sessions" description="Devices currently signed in to your account." />
      <ul className="divide-y divide-line">
        {(sessions.data ?? []).map((s) => (
          <li key={s.id} className="flex items-center justify-between gap-3 px-4 py-2.5 text-[13px]">
            <div className="min-w-0">
              <p className="flex items-center gap-2 font-medium">
                {describe(s.user_agent)} {s.current ? <Badge tone="accent">This device</Badge> : null}
              </p>
              <p className="text-[12px] text-muted">
                Signed in {formatRelative(s.created_at)}
                {s.ip_address ? <> · <span className="font-mono">{s.ip_address}</span></> : null}
              </p>
            </div>
            {!s.current ? (
              <Button size="sm" variant="ghost" loading={revoke.isPending && revoke.variables === s.id} onClick={() => revoke.mutate(s.id)}>
                Sign out
              </Button>
            ) : null}
          </li>
        ))}
      </ul>
    </Card>
  );
}
