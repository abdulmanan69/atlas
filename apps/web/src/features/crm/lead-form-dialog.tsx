import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { FormCombobox } from "@/components/ui/combobox";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Avatar, Checkbox, Input, Label } from "@/components/ui/primitives";
import { FormSelect, SelectItem } from "@/components/ui/select";
import { useInvalidate, useMembers, useStages, type Lead } from "@/features/queries";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api";
import { fullName } from "@/lib/format";
import { leadSchema, type LeadFormInput as Input_, type LeadFormOutput as Output } from "@/features/crm/lead-schema";

function toDefaults(lead?: Lead): Input_ {
  return {
    first_name: lead?.first_name ?? "",
    last_name: lead?.last_name ?? "",
    phone: lead?.phone_e164 ?? "",
    email: lead?.email ?? "",
    company: lead?.company ?? "",
    job_title: lead?.job_title ?? "",
    source: lead?.source ?? "",
    tags: lead?.tags.join(", ") ?? "",
    owner_user_id: lead?.owner_user_id ?? "",
    stage_id: lead?.stage_id ?? "",
    do_not_call: lead?.do_not_call ?? false,
  };
}

export function LeadFormDialog({ open, onOpenChange, lead }: { open: boolean; onOpenChange: (o: boolean) => void; lead?: Lead }) {
  const editing = Boolean(lead);
  const stages = useStages();
  const members = useMembers();
  const invalidate = useInvalidate();
  const navigate = useNavigate();
  const form = useForm<Input_, unknown, Output>({ resolver: zodResolver(leadSchema), defaultValues: toDefaults(lead) });

  useEffect(() => {
    if (open) form.reset(toDefaults(lead));
  }, [open, lead, form]);

  const save = useMutation({
    mutationFn: async (v: Output) => {
      if (lead) {
        const { stage_id: _stage, ...rest } = v;
        return unwrap(api.PATCH("/api/v1/leads/{lead_id}", { params: { path: { lead_id: lead.id } }, body: rest }));
      }
      return unwrap(api.POST("/api/v1/leads", { body: { ...v, custom_fields: {}, consent_status: "unknown" } }));
    },
    onSuccess: (saved) => {
      toast.success(editing ? "Lead updated" : `Added ${fullName(saved.first_name, saved.last_name)}`);
      void invalidate();
      onOpenChange(false);
      if (!editing) void navigate(`/leads/${saved.id}`);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 422) {
        const fields = err.fieldErrors();
        const detailField = (err.details as { field?: string } | null)?.field;
        if (detailField === "phone") form.setError("phone", { message: err.message });
        for (const [k, m] of Object.entries(fields)) {
          if (k in leadSchema.shape) form.setError(k as keyof Input_, { message: m });
        }
      }
    },
  });

  const dup = save.error instanceof ApiError && save.error.status === 409 ? (save.error.details as { existing_lead_id?: string } | null) : null;
  const e = form.formState.errors;
  const submit = form.handleSubmit((v) => save.mutate(v));

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit lead" : "New lead"}
      description={editing ? undefined : "Phone numbers are checked and stored in international format."}
      width="max-w-xl"
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" loading={save.isPending} onClick={submit}>
            {editing ? "Save changes" : "Add lead"}
          </Button>
        </>
      }
    >
      <form className="grid grid-cols-1 gap-4 sm:grid-cols-2" noValidate onSubmit={submit}>
        {save.error && !(save.error instanceof ApiError && save.error.status === 422) ? (
          <div className="sm:col-span-2">
            <FormError message={errorMessage(save.error)} />
            {dup?.existing_lead_id ? (
              <Link to={`/leads/${dup.existing_lead_id}`} onClick={() => onOpenChange(false)} className="mt-1.5 inline-block text-[13px] font-medium text-accent hover:underline">
                Open the existing lead
              </Link>
            ) : null}
          </div>
        ) : null}
        <Field label="First name" required error={e.first_name?.message}>
          <Input autoFocus {...form.register("first_name")} />
        </Field>
        <Field label="Last name" error={e.last_name?.message}>
          <Input {...form.register("last_name")} />
        </Field>
        <Field label="Phone" hint="Include the country code for numbers abroad" error={e.phone?.message}>
          <Input type="tel" inputMode="tel" placeholder="+44 20 7946 0000" className="font-mono" {...form.register("phone")} />
        </Field>
        <Field label="Email" error={e.email?.message}>
          <Input type="email" {...form.register("email")} />
        </Field>
        <Field label="Company" error={e.company?.message}>
          <Input {...form.register("company")} />
        </Field>
        <Field label="Job title" error={e.job_title?.message}>
          <Input {...form.register("job_title")} />
        </Field>
        <Field label="Source" hint="Where this lead came from" error={e.source?.message}>
          <Input placeholder="web_form" className="font-mono" {...form.register("source")} />
        </Field>
        <Field label="Tags" hint="Separate with commas" error={e.tags?.message}>
          <Input placeholder="vip, q4" {...form.register("tags")} />
        </Field>
        <Field label="Owner">
          <FormCombobox
            control={form.control}
            name="owner_user_id"
            placeholder="Unassigned"
            searchPlaceholder="Search teammates"
            allowClear
            options={(members.data ?? []).map((m) => ({
              value: m.user.id,
              label: m.user.full_name,
              description: m.user.email,
              icon: <Avatar name={m.user.full_name} className="size-5 text-[9px]" />,
            }))}
          />
        </Field>
        {!editing ? (
          <Field label="Stage">
            <FormSelect control={form.control} name="stage_id" allowEmpty emptyLabel={`Default (${stages.data?.find((s) => s.is_default)?.name ?? "New"})`}>
              {stages.data?.map((s) => (
                <SelectItem key={s.id} value={s.id}>
                  {s.name}
                </SelectItem>
              ))}
            </FormSelect>
          </Field>
        ) : null}
        <div className="flex items-start gap-2.5 sm:col-span-2">
          <Checkbox
            aria-label="Do not call"
            checked={form.watch("do_not_call")}
            onCheckedChange={(v) => form.setValue("do_not_call", v, { shouldDirty: true })}
            className="mt-0.5"
          />
          <div>
            <Label>Do not call</Label>
            <p className="text-[12px] text-muted">Agents and campaigns will never call this lead.</p>
          </div>
        </div>
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}
