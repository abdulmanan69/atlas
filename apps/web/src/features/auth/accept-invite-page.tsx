import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link, useNavigate, useSearchParams } from "react-router";
import { z } from "zod";

import { FullPageSpinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Field, FormError } from "@/components/ui/form";
import { Input } from "@/components/ui/primitives";
import { AuthLayout } from "@/features/auth/auth-layout";
import { api, errorMessage, unwrap } from "@/lib/api";
import { session, useSession } from "@/lib/session";

const schema = z.object({
  full_name: z.string().trim().min(1, "Enter your name").max(200),
  password: z.string().min(12, "Use at least 12 characters").max(128),
});
type Values = z.infer<typeof schema>;

export default function AcceptInvitePage() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const status = useSession((s) => s.status);
  const user = useSession((s) => s.user);
  const navigate = useNavigate();
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { full_name: "", password: "" } });

  const accept = useMutation({
    mutationFn: (body: Partial<Values>) => unwrap(api.POST("/api/v1/auth/invitations/accept", { body: { token, ...body } })),
    onSuccess: (tokens) => {
      session.getState().signIn(tokens);
      const joined = tokens.memberships[tokens.memberships.length - 1];
      if (joined) session.getState().selectOrg(joined.organization.id);
      void navigate("/", { replace: true });
    },
  });

  if (status === "unknown") return <FullPageSpinner label="Checking your invitation" />;
  if (!token) {
    return (
      <AuthLayout title="Invitation link incomplete" subtitle="Open the full link from your invitation, or ask the sender for a new one.">
        <Button asChild>
          <Link to="/sign-in">Go to sign in</Link>
        </Button>
      </AuthLayout>
    );
  }

  if (status === "authenticated" && user) {
    return (
      <AuthLayout title="Join the organization" subtitle={<>You're signed in as <strong className="text-ink">{user.email}</strong>. The invitation must match this email.</>}>
        <FormError message={accept.error ? errorMessage(accept.error) : null} />
        <Button variant="primary" size="lg" className="mt-4 w-full" loading={accept.isPending} onClick={() => accept.mutate({})}>
          Accept invitation
        </Button>
      </AuthLayout>
    );
  }

  const next = encodeURIComponent(`/invite?token=${token}`);
  return (
    <AuthLayout
      title="Join your team"
      subtitle="Set up your account to accept the invitation."
      footer={
        <>
          Already have an account?{" "}
          <Link to={`/sign-in?next=${next}`} className="font-medium text-accent hover:underline">
            Sign in to accept
          </Link>
        </>
      }
    >
      <form className="flex flex-col gap-4" noValidate onSubmit={form.handleSubmit((v) => accept.mutate(v))}>
        <FormError message={accept.error ? errorMessage(accept.error) : null} />
        <Field label="Your name" error={form.formState.errors.full_name?.message}>
          <Input autoComplete="name" autoFocus {...form.register("full_name")} />
        </Field>
        <Field label="Password" hint="At least 12 characters." error={form.formState.errors.password?.message}>
          <Input type="password" autoComplete="new-password" {...form.register("password")} />
        </Field>
        <Button type="submit" variant="primary" size="lg" className="w-full" loading={accept.isPending}>
          Create account and join
        </Button>
      </form>
    </AuthLayout>
  );
}
