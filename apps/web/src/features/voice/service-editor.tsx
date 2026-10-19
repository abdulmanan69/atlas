/**
 * Choose a connection for one service (LLM, STT, TTS, embeddings) and edit its
 * settings. Settings fields are generated from the provider's JSON Schema, so a
 * new provider needs no UI changes: enums become a Select, a voice catalog becomes
 * a searchable Combobox backed by the voices API.
 */
import { useQuery } from "@tanstack/react-query";
import { Pause, Play } from "lucide-react";
import { useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
import { Checkbox, Input, Label } from "@/components/ui/primitives";
import { Select, SelectItem } from "@/components/ui/select";
import { useOrgId } from "@/features/queries";
import { PocketVoicePicker } from "@/features/voice/pocket-voice-picker";
import { connectionsFor, providerOf, vk, type Catalog, type Connection, type ServiceChoice, type ServiceKind } from "@/features/voice/queries";
import { isPocketConnection } from "@/features/voice/voice-cloning";
import { api, unwrap } from "@/lib/api";
import { useDebounced } from "@/lib/hooks";

type Value = string | number | boolean | null;
interface PropSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  enum?: Value[];
  examples?: Value[];
  default?: Value;
  minimum?: number;
  maximum?: number;
  voice_catalog?: string;
  allow_custom_input?: boolean;
}

const HIDDEN = new Set(["provider", "api_key", "base_url"]);
const ORDER = ["model", "voice", "language", "temperature", "speed"];

function propsOf(schema: Record<string, unknown> | undefined): [string, PropSchema][] {
  const props = (schema?.properties ?? {}) as Record<string, PropSchema>;
  return Object.entries(props)
    .filter(([k]) => !HIDDEN.has(k))
    .sort(([a], [b]) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99))
    .slice(0, 8);
}

const typeOf = (p: PropSchema) => (Array.isArray(p.type) ? p.type.find((t) => t !== "null") : p.type) ?? "string";

export function ServiceEditor({
  kind,
  value,
  onChange,
  catalog,
  connections,
  disabled,
  allowNone,
}: {
  kind: ServiceKind;
  value: ServiceChoice | null;
  onChange: (v: ServiceChoice | null) => void;
  catalog: Catalog | undefined;
  connections: Connection[] | undefined;
  disabled?: boolean;
  allowNone?: boolean;
}) {
  const options = connectionsFor(kind, catalog, connections);
  const connection = options.find((c) => c.id === value?.connection_id);
  const provider = providerOf(kind, catalog, connection);
  const pocket = kind === "tts" && isPocketConnection(connection);
  const id = useId();

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${id}-conn`}>Provider</Label>
        <Select
          id={`${id}-conn`}
          disabled={disabled}
          value={value?.connection_id ?? ""}
          onValueChange={(v) => onChange(v ? { connection_id: v, settings: {} } : null)}
          allowEmpty={allowNone}
          emptyLabel="Not used"
          placeholder={options.length ? "Choose a connection…" : "Add a connection first"}
        >
          {options.map((c) => (
            <SelectItem key={c.id} value={c.id} description={c.provider_title}>
              {c.name}
            </SelectItem>
          ))}
        </Select>
      </div>
      {value && provider ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {propsOf(provider.settings_schema).map(([name, prop]) => (
            <SettingField
              key={name}
              name={name}
              prop={prop}
              disabled={disabled}
              value={(value.settings?.[name] as Value | undefined) ?? null}
              provider={provider.id}
              connectionId={value.connection_id}
              pocket={pocket}
              onChange={(v) => onChange({ ...value, settings: { ...value.settings, [name]: v } })}
            />
          ))}
        </div>
      ) : null}
      {value && connection?.hosted_trial ? (
        <p className="text-[12px] text-muted">Hosted trial models choose their own defaults.</p>
      ) : null}
    </div>
  );
}

function SettingField({
  name,
  prop,
  value,
  onChange,
  disabled,
  provider,
  connectionId,
  pocket,
}: {
  name: string;
  prop: PropSchema;
  value: Value;
  onChange: (v: Value) => void;
  disabled?: boolean;
  provider: string;
  connectionId: string;
  /** A Pocket TTS connection: its voices (built-in and cloned) are listed by ATLAS. */
  pocket?: boolean;
}) {
  const id = useId();
  const label = prop.title ?? name.replace(/_/g, " ");
  const t = typeOf(prop);
  const placeholder = prop.default !== undefined && prop.default !== null ? `Default: ${String(prop.default)}` : undefined;

  if (pocket && name === "voice") {
    return <PocketVoicePicker label={label} value={(value as string | null) ?? ""} onChange={onChange} connectionId={connectionId} placeholder={placeholder} disabled={disabled} />;
  }

  if (prop.voice_catalog) {
    return <VoicePicker label={label} value={(value as string | null) ?? ""} onChange={onChange} provider={provider} connectionId={connectionId} placeholder={placeholder} disabled={disabled} />;
  }
  let control;
  if (prop.enum && prop.enum.length) {
    control = (
      <Select
        id={id}
        disabled={disabled}
        value={value === null ? "" : String(value)}
        onValueChange={(v) => onChange(v === "" ? null : v)}
        allowEmpty
        emptyLabel={placeholder ?? "Default"}
      >
        {prop.enum
          .filter((v) => v !== null && v !== "")
          .map((v) => (
            <SelectItem key={String(v)} value={String(v)}>
              {String(v)}
            </SelectItem>
          ))}
      </Select>
    );
  } else if (t === "boolean") {
    return (
      <label className="flex items-center gap-2 self-end pb-1.5 text-[13px]">
        <Checkbox aria-label={label} checked={value === true} onCheckedChange={(v) => onChange(v)} />
        <span className="capitalize text-ink-2">{label}</span>
      </label>
    );
  } else if (t === "number" || t === "integer") {
    control = (
      <Input
        id={id}
        type="number"
        disabled={disabled}
        step={t === "integer" ? 1 : 0.1}
        min={prop.minimum}
        max={prop.maximum}
        placeholder={placeholder}
        value={value === null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
        className="font-mono"
      />
    );
  } else {
    const listId = `${id}-examples`;
    control = (
      <>
        <Input
          id={id}
          list={prop.examples?.length ? listId : undefined}
          disabled={disabled}
          placeholder={placeholder}
          value={value === null ? "" : String(value)}
          onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
          className="font-mono"
        />
        {prop.examples?.length ? (
          <datalist id={listId}>
            {prop.examples.map((ex) => (
              <option key={String(ex)} value={String(ex)} />
            ))}
          </datalist>
        ) : null}
      </>
    );
  }
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id} className="capitalize">
        {label}
      </Label>
      {control}
      {prop.description ? <p className="line-clamp-2 text-[11.5px] text-muted">{prop.description}</p> : null}
    </div>
  );
}

function VoicePicker({
  label,
  value,
  onChange,
  provider,
  connectionId,
  placeholder,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (v: Value) => void;
  provider: string;
  connectionId: string;
  placeholder?: string;
  disabled?: boolean;
}) {
  const o = useOrgId();
  const [q, setQ] = useState("");
  const query = useDebounced(q.trim(), 300);
  const [playing, setPlaying] = useState<string | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  const id = useId();
  const voices = useQuery({
    queryKey: vk.voices(o, provider, connectionId, query),
    queryFn: () => unwrap(api.GET("/api/v1/ai/voices", { params: { query: { provider, connection_id: connectionId, q: query || undefined } } })),
    staleTime: 5 * 60_000,
  });
  const list = voices.data ?? [];
  const selected = list.find((v) => v.id === value);

  const preview = (url: string, vid: string) => {
    audio.current?.pause();
    if (playing === vid) {
      setPlaying(null);
      return;
    }
    audio.current = new Audio(url);
    audio.current.onended = () => setPlaying(null);
    void audio.current.play();
    setPlaying(vid);
  };

  const options: ComboboxOption[] = list.map((v) => ({
    value: v.id,
    label: v.name,
    description: [v.gender, v.accent, v.language].filter(Boolean).join(" · ") || undefined,
    trailing: v.preview_url ? (
      <Button
        type="button"
        size="icon-sm"
        variant="ghost"
        aria-label={`Preview ${v.name}`}
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation();
          preview(v.preview_url!, v.id);
        }}
      >
        {playing === v.id ? <Pause /> : <Play />}
      </Button>
    ) : undefined,
  }));

  return (
    <div className="flex flex-col gap-1.5 sm:col-span-2">
      <Label htmlFor={id} className="capitalize">
        {label}
      </Label>
      <div className="flex gap-2">
        <Combobox
          id={id}
          className="min-w-0 flex-1"
          disabled={disabled}
          value={value || null}
          valueLabel={value}
          onValueChange={(v) => onChange(v)}
          options={options}
          loading={voices.isFetching}
          onSearchChange={setQ}
          placeholder={placeholder ?? "Search voices"}
          searchPlaceholder="Search by name, accent or language"
          emptyText={voices.isError ? "Voices couldn't be loaded." : "No voices match."}
          allowClear
        />
        {selected?.preview_url ? (
          <Button type="button" size="icon" aria-label={`Preview ${selected.name}`} aria-pressed={playing === selected.id} onClick={() => preview(selected.preview_url!, selected.id)}>
            {playing === selected.id ? <Pause /> : <Play />}
          </Button>
        ) : null}
      </div>
      <Input
        aria-label="Voice ID"
        disabled={disabled}
        value={value}
        placeholder="Or paste a voice ID"
        onChange={(e) => onChange(e.target.value || null)}
        className="font-mono"
      />
      <p className="text-[11.5px] text-muted">{selected ? `Selected: ${selected.name}` : "Search the catalog or paste a voice ID."}</p>
    </div>
  );
}
