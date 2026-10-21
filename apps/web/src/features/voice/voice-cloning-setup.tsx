/**
 * Turning voice cloning on for a Pocket TTS connection's server. Owners paste a Hugging Face
 * read token of an account that accepted the gated weights' terms; the voice server checks
 * it, downloads the cloning weights and reports progress, which this view polls. Everyone
 * else sees the state and who can change it. ATLAS never stores the token.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ExternalLink, KeyRound, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Field, FormError } from "@/components/ui/form";
import { Checkbox, Input, Skeleton } from "@/components/ui/primitives";
import { useOrgId } from "@/features/queries";
import { CLONING_TERMS_URL, cloningStatusKey, enableCloning, useCloningStatus } from "@/features/voice/voice-cloning";
import { ApiError, errorMessage } from "@/lib/api";

const TOKEN_SHAPE = /^hf_[A-Za-z0-9_-]{20,200}$/;

export function VoiceCloningSetup({ connectionId, onReady }: { connectionId: string; onReady: () => void }) {
  const status = useCloningStatus(connectionId);
  const state = status.data?.state;

  useEffect(() => {
    if (state === "on") onReady();
  }, [state, onReady]);

  if (status.isLoading) return <Skeleton className="h-40" />;
  if (status.isError) return <FormError message={`The voice server couldn't be reached: ${errorMessage(status.error)}`} />;
  const s = status.data!;

  if (s.state === "unsupported") {
    return (
      <p role="status" className="rounded-md border border-warn/30 bg-warn-soft px-3 py-2 text-[13px] text-warn">
        {s.message ?? "This voice server is too old to turn voice cloning on from here. Update it, then try again."}
      </p>
    );
  }
  if (s.state === "enabling") {
    return (
      <div role="status" aria-live="polite" className="flex flex-col items-center gap-2 rounded-lg border border-line bg-surface-2/50 px-4 py-8 text-center">
        <Loader2 className="size-5 animate-spin text-accent" aria-hidden />
        <p className="text-[13.5px] font-medium">Turning voice cloning on…</p>
        <p className="max-w-sm text-[12.5px] text-muted">
          The voice server is downloading the voice-cloning model once (about 0.2 GB). Built-in voices keep working meanwhile. This usually takes a minute or two.
        </p>
      </div>
    );
  }
  if (s.state === "on") {
    return (
      <p role="status" className="flex items-center gap-2 text-[13px] text-ink-2">
        <CheckCircle2 className="size-4 text-success" aria-hidden /> Voice cloning is on.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <p className="text-[13px] text-ink-2">
        Voices are cloned on your own voice server. The voice-cloning model is free, but its makers (Kyutai) hand it out only to people who accepted their rules on Hugging Face,
        which forbid cloning anyone without their consent. It is downloaded once; after that no recording leaves your server.
      </p>
      {s.state === "failed" && s.message ? (
        <p role="alert" className="rounded-md border border-danger/30 bg-danger-soft/50 px-3 py-2 text-[12.5px] text-danger">
          {s.message}
        </p>
      ) : null}
      {s.can_manage ? (
        <EnableForm connectionId={connectionId} termsUrl={s.terms_url || CLONING_TERMS_URL} />
      ) : (
        <p role="status" className="rounded-md border border-line bg-surface-2/60 px-3 py-2 text-[13px] text-ink-2">
          Voice cloning is turned on by an organization owner (on shared hosting, by the platform&apos;s operator). Ask them to set it up; the steps are shown to them here.
        </p>
      )}
    </div>
  );
}

function EnableForm({ connectionId, termsUrl }: { connectionId: string; termsUrl: string }) {
  const qc = useQueryClient();
  const o = useOrgId();
  const [token, setToken] = useState("");
  const [terms, setTerms] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const enable = useMutation({
    mutationFn: () => enableCloning(connectionId, token),
    onSuccess: (next) => {
      setToken("");
      qc.setQueryData(cloningStatusKey(o, connectionId), next);
    },
  });

  const tokenError = !submitted ? undefined : !token.trim() ? "Paste the token." : !TOKEN_SHAPE.test(token.trim()) ? "A Hugging Face token starts with hf_ and has no spaces." : undefined;
  const termsError = submitted && !terms ? "Confirm that the token's account accepted the terms." : undefined;
  const serverError = enable.error instanceof ApiError && enable.error.status === 422 ? errorMessage(enable.error) : null;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (!token.trim() || !TOKEN_SHAPE.test(token.trim()) || !terms) return;
    enable.mutate();
  };

  return (
    <form className="flex flex-col gap-4" onSubmit={submit} noValidate>
      <ol className="list-decimal space-y-1.5 pl-5 text-[13px] text-ink-2">
        <li>
          Sign in at Hugging Face and accept the terms of{" "}
          <a href={termsUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 font-medium text-accent hover:underline">
            kyutai/pocket-tts <ExternalLink className="size-3" aria-hidden />
          </a>
          .
        </li>
        <li>
          Create a <strong>Read</strong> token at{" "}
          <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 font-medium text-accent hover:underline">
            huggingface.co/settings/tokens <ExternalLink className="size-3" aria-hidden />
          </a>{" "}
          and paste it below.
        </li>
      </ol>
      {enable.error && !serverError ? <FormError message={errorMessage(enable.error)} /> : null}
      <Field label="Hugging Face access token" required error={tokenError ?? serverError ?? undefined} hint="Sent only to your voice server, which keeps it to download the model again after a restart. ATLAS never stores it.">
        <Input
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="hf_…"
          className="font-mono"
        />
      </Field>
      <label className="flex items-start gap-2 text-[13px] font-medium">
        <Checkbox aria-label="The account that owns this token accepted the terms of kyutai/pocket-tts" checked={terms} onCheckedChange={setTerms} className="mt-0.5" />
        <span>The account that owns this token accepted the terms of kyutai/pocket-tts. I will clone only voices whose speakers agreed.</span>
      </label>
      {termsError ? (
        <p role="alert" className="-mt-2 text-[12px] text-danger">
          {termsError}
        </p>
      ) : null}
      <div>
        <Button type="submit" variant="primary" loading={enable.isPending}>
          <KeyRound /> Turn on voice cloning
        </Button>
      </div>
    </form>
  );
}
