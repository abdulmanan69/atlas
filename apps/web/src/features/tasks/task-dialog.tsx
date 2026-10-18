import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { FormCombobox } from "@/components/ui/combobox";
import { FormDateTimePicker } from "@/components/ui/date-time-picker";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Avatar, Input, Textarea } from "@/components/ui/primitives";
import { useInvalidate, useMembers } from "@/features/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { useSession } from "@/lib/session";

const schema = z.object({
  title: z.string().trim().min(1, "Describe the task").max(300),
  description: z.string().max(10_000),
  due_at: z.string(),
  assignee_user_id: z.string(),
});
type Values = z.infer<typeof schema>;

export function TaskDialog({ open, onOpenChange, leadId }: { open: boolean; onOpenChange: (o: boolean) => void; leadId?: string }) {
  const me = useSession((s) => s.user?.id ?? "");
  const members = useMembers();
  const invalidate = useInvalidate();
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { title: "", description: "", due_at: "", assignee_user_id: me },
  });
  useEffect(() => {
    if (open) form.reset({ title: "", description: "", due_at: "", assignee_user_id: me });
  }, [open, me, form]);

  const create = useMutation({
    mutationFn: (v: Values) =>
      unwrap(
        api.POST("/api/v1/tasks", {
          body: {
            title: v.title,
            description: v.description || null,
            due_at: v.due_at ? new Date(v.due_at).toISOString() : null,
            assignee_user_id: v.assignee_user_id || null,
            lead_id: leadId ?? null,
          },
        }),
      ),
    onSuccess: () => {
      toast.success("Task created");
      void invalidate();
      onOpenChange(false);
    },
  });
  const submit = form.handleSubmit((v) => create.mutate(v));

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="New task"
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" loading={create.isPending} onClick={submit}>
            Create task
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-4" noValidate onSubmit={submit}>
        <FormError message={create.error ? errorMessage(create.error) : null} />
        <Field label="Task" required error={form.formState.errors.title?.message}>
          <Input autoFocus placeholder="Send pricing brochure" {...form.register("title")} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Due" hint="Leave empty for no due date">
            <FormDateTimePicker control={form.control} name="due_at" clearable />
          </Field>
          <Field label="Assignee">
            <FormCombobox
              control={form.control}
              name="assignee_user_id"
              placeholder="Unassigned"
              searchPlaceholder="Search teammates"
              allowClear
              options={
                members.data
                  ? members.data.map((m) => ({
                      value: m.user.id,
                      label: m.user.id === me ? `${m.user.full_name} (you)` : m.user.full_name,
                      description: m.user.email,
                      icon: <Avatar name={m.user.full_name} className="size-5 text-[9px]" />,
                    }))
                  : [{ value: me, label: "You" }]
              }
            />
          </Field>
        </div>
        <Field label="Details">
          <Textarea {...form.register("description")} />
        </Field>
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}
