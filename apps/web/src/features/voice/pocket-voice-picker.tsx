/**
 * Voice setting for Pocket TTS connections: built-in voices plus this organization's cloned
 * voices (badged "Cloned"), "Clone a voice" for owners and admins, and delete with
 * confirmation.
 */
import { useMutation } from "@tanstack/react-query";
import { Mic, Trash2 } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Badge, Input, Label } from "@/components/ui/primitives";
import { useInvalidate } from "@/features/queries";
import { CloneVoiceDialog } from "@/features/voice/clone-voice-dialog";
import { deleteVoice, usePocketVoices, type PocketVoice } from "@/features/voice/voice-cloning";
import { errorMessage } from "@/lib/api";
import { useHasRole } from "@/lib/session";

export function PocketVoicePicker({
  label,
  value,
  onChange,
  connectionId,
  placeholder,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (v: string | null) => void;
  connectionId: string;
  placeholder?: string;
  disabled?: boolean;
}) {
  const id = useId();
  const voices = usePocketVoices(connectionId);
  const canManage = useHasRole("admin") && !disabled;
  const invalidate = useInvalidate();
  const [cloning, setCloning] = useState(false);
  const [removing, setRemoving] = useState<PocketVoice | null>(null);

  const remove = useMutation({
    mutationFn: (v: PocketVoice) => deleteVoice(connectionId, v.id),
    onSuccess: (_, v) => {
      toast.success(`Deleted the voice “${v.name}”.`);
      if (value === v.id) onChange(null);
      setRemoving(null);
      void invalidate("ai", "pocket-voices", connectionId);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const list = voices.data?.voices ?? [];
  const selected = list.find((v) => v.id === value);
  const options: ComboboxOption[] = list.map((v) => ({
    value: v.id,
    label: v.name,
    keywords: [v.id, v.cloned ? "cloned" : "built-in"],
    description: v.cloned
      ? `Cloned${v.sample_seconds ? ` from a ${Math.round(v.sample_seconds)} s sample` : ""}`
      : [v.default ? "Default" : null, v.license ? `Recording ${v.license}` : null].filter(Boolean).join(" · ") || undefined,
    trailing: v.cloned ? (
      <span className="flex items-center gap-1">
        <Badge tone="accent">Cloned</Badge>
        {canManage ? (
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            aria-label={`Delete ${v.name}`}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              setRemoving(v);
            }}
          >
            <Trash2 />
          </Button>
        ) : null}
      </span>
    ) : undefined,
  }));

  return (
    <div className="flex flex-col gap-1.5 sm:col-span-2">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={id} className="capitalize">
          {label}
        </Label>
        {canManage && voices.data ? (
          <Button type="button" size="sm" variant="ghost" onClick={() => setCloning(true)}>
            <Mic /> Clone a voice
          </Button>
        ) : null}
      </div>
      {voices.isError ? (
        <Input id={id} disabled={disabled} value={value} placeholder={placeholder ?? "Voice name, for example alba"} onChange={(e) => onChange(e.target.value || null)} className="font-mono" />
      ) : (
        <Combobox
          id={id}
          disabled={disabled}
          value={value || null}
          valueLabel={selected?.name ?? value}
          onValueChange={onChange}
          options={options}
          loading={voices.isLoading}
          placeholder={placeholder ?? "Choose a voice"}
          searchPlaceholder="Search voices"
          emptyText="No voices match."
          allowClear
        />
      )}
      {voices.isError ? (
        <p className="text-[11.5px] text-danger">Voices couldn't be loaded: {errorMessage(voices.error)}</p>
      ) : (
        <p className="flex flex-wrap items-center gap-1.5 text-[11.5px] text-muted">
          {selected ? (
            <>
              Selected: {selected.name}
              {selected.cloned ? <Badge tone="accent">Cloned</Badge> : null}
            </>
          ) : (
            "Built-in voices are cleared for commercial use. Cloned voices need the speaker's permission."
          )}
          {voices.data && !voices.data.cloning_available && canManage ? <span>· Voice cloning isn't set up on this server.</span> : null}
        </p>
      )}
      {cloning && voices.data ? (
        <CloneVoiceDialog
          open
          onOpenChange={(o) => !o && setCloning(false)}
          connectionId={connectionId}
          available={voices.data.cloning_available}
          setupMessage={voices.data.setup_message}
          onCreated={(v) => onChange(v.id)}
        />
      ) : null}
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={`Delete the voice “${removing?.name ?? ""}”?`}
        description="Agents can no longer speak with it. This can't be undone; cloning it again needs a new sample. Voices in use by the models or an agent can't be deleted."
        confirmLabel="Delete voice"
        loading={remove.isPending}
        onConfirm={() => removing && remove.mutate(removing)}
      />
    </div>
  );
}
