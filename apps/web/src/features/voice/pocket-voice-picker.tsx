/**
 * Voice setting for Pocket TTS, expressive and voice design connections: built-in voices
 * plus this organization's own (badged "Cloned" or "Designed"), "Clone a voice" and, when
 * the server can, "Design a voice" for owners and admins, delete with confirmation, and
 * whether voice cloning is on (owners can turn it off). Voices of an expressive server are
 * badged with the sounds they make ("Laughs, sighs").
 */
import { useMutation } from "@tanstack/react-query";
import { Mic, Sparkles, Trash2 } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Badge, Input, Label } from "@/components/ui/primitives";
import { soundSummary } from "@/features/calls/sound-cues";
import { useInvalidate } from "@/features/queries";
import { CloneVoiceDialog } from "@/features/voice/clone-voice-dialog";
import { DesignVoiceDialog } from "@/features/voice/design-voice-dialog";
import { deleteVoice, disableCloning, isCustomVoice, usePocketVoices, useVoiceCapabilities, type PocketVoice } from "@/features/voice/voice-cloning";
import { errorMessage } from "@/lib/api";
import { useHasRole } from "@/lib/session";

function OwnBadge({ voice }: { voice: PocketVoice }) {
  return <Badge tone="accent">{voice.designed ? "Designed" : "Cloned"}</Badge>;
}

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
  const capabilities = useVoiceCapabilities(connectionId);
  const canManage = useHasRole("admin") && !disabled;
  const isOwner = useHasRole("owner") && !disabled;
  const invalidate = useInvalidate();
  const [cloning, setCloning] = useState(false);
  const [designing, setDesigning] = useState(false);
  const [removing, setRemoving] = useState<PocketVoice | null>(null);
  const [turningOff, setTurningOff] = useState(false);

  const turnOff = useMutation({
    mutationFn: () => disableCloning(connectionId),
    onSuccess: () => {
      toast.success("Voice cloning is off. Cloned voices stay on the server and come back when it is turned on again.");
      setTurningOff(false);
      void invalidate("ai", "pocket-voices", connectionId);
      void invalidate("ai", "voice-cloning", connectionId);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const cloningState = voices.data?.cloning_state ?? (voices.data?.cloning_available ? "on" : "off");

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
  const sounds = voices.data?.sound_tags ?? [];
  const expressive = sounds.length ? (
    <Badge tone="success" title={`Can make these sounds: ${sounds.join(", ")}`}>
      {soundSummary(sounds)}
    </Badge>
  ) : null;
  const options: ComboboxOption[] = list.map((v) => ({
    value: v.id,
    label: v.name,
    keywords: [v.id, v.designed ? "designed" : v.cloned ? "cloned" : "built-in", ...(sounds.length ? ["expressive", ...sounds] : [])],
    description: v.designed
      ? v.description || "Designed from a description"
      : v.cloned
        ? `Cloned${v.sample_seconds ? ` from a ${Math.round(v.sample_seconds)} s sample` : ""}`
        : [v.default ? "Default" : null, v.license ? `Recording ${v.license}` : null].filter(Boolean).join(" · ") || undefined,
    trailing: isCustomVoice(v) ? (
      <span className="flex items-center gap-1">
        {expressive}
        <OwnBadge voice={v} />
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
    ) : (
      (expressive ?? undefined)
    ),
  }));

  return (
    <div className="flex flex-col gap-1.5 sm:col-span-2">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={id} className="capitalize">
          {label}
        </Label>
        {canManage && voices.data ? (
          <span className="flex items-center gap-1">
            {capabilities.data?.design ? (
              <Button type="button" size="sm" variant="ghost" onClick={() => setDesigning(true)}>
                <Sparkles /> Design a voice
              </Button>
            ) : null}
            <Button type="button" size="sm" variant="ghost" onClick={() => setCloning(true)}>
              <Mic /> Clone a voice
            </Button>
          </span>
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
              {expressive}
              {isCustomVoice(selected) ? <OwnBadge voice={selected} /> : null}
            </>
          ) : (
            "Built-in voices are cleared for commercial use. Cloned voices need the speaker's permission."
          )}
          {voices.data && canManage ? (
            cloningState === "on" ? (
              <span className="flex items-center gap-1">
                · Voice cloning is on
                {isOwner ? (
                  <Button type="button" size="sm" variant="link" className="h-auto px-0 text-[11.5px]" onClick={() => setTurningOff(true)}>
                    Turn off
                  </Button>
                ) : null}
              </span>
            ) : cloningState === "enabling" ? (
              <span>· Voice cloning is being turned on…</span>
            ) : (
              <span>· Voice cloning is off for this server. Clone a voice to turn it on.</span>
            )
          ) : null}
        </p>
      )}
      {cloning && voices.data ? (
        <CloneVoiceDialog
          open
          onOpenChange={(o) => !o && setCloning(false)}
          connectionId={connectionId}
          available={voices.data.cloning_available}
          onCreated={(v) => onChange(v.id)}
        />
      ) : null}
      {designing && capabilities.data ? (
        <DesignVoiceDialog
          open
          onOpenChange={(o) => !o && setDesigning(false)}
          connectionId={connectionId}
          capabilities={capabilities.data}
          onCreated={(v) => onChange(v.id)}
        />
      ) : null}
      <ConfirmDialog
        open={turningOff}
        onOpenChange={(o) => !o && setTurningOff(false)}
        title="Turn voice cloning off?"
        description="The voice server forgets its Hugging Face token. Agents using cloned voices can no longer speak with them until it is turned on again; the voices themselves are kept. This applies to every organization using this voice server."
        confirmLabel="Turn off"
        loading={turnOff.isPending}
        onConfirm={() => turnOff.mutate()}
      />
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={`Delete the voice “${removing?.name ?? ""}”?`}
        description={`Agents can no longer speak with it. This can't be undone; ${removing?.designed ? "it would have to be designed again" : "cloning it again needs a new sample"}. Voices in use by the models or an agent can't be deleted.`}
        confirmLabel="Delete voice"
        loading={remove.isPending}
        onConfirm={() => removing && remove.mutate(removing)}
      />
    </div>
  );
}
