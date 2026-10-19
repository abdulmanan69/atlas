import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link, useNavigate } from "react-router";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Field, FormError } from "@/components/ui/form";
import { Input } from "@/components/ui/primitives";
import { AuthLayout } from "@/features/auth/auth-layout";
import { useAuthOptions } from "@/features/auth/auth-options";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api";
import { session } from "@/lib/session";

const signUpSchema = z.object({
  full_name: z.string().trim().min(1, "Enter your name").max(200),
  email: z.email("Enter a valid email address"),
  password: z.string().min(12, "Use at least 12 characters").max(128),
  organization_name: z.string().trim().min(2, "Use at least 2 characters").max(200),
});
type Values = z.infer<typeof signUpSchema>;

export default function SignUpPage() {
  const navigate = useNavigate();
  const form = useForm<Values>({
    resolver: zodResolver(signUpSchema),
    defaultValues: { full_name: "", email: "", password: "", organization_name: "" },
  });
  const signup = useMutation({
    mutationFn: (body: Values) => unwrap(api.POST("/api/v1/auth/signup", { body })),
    onSuccess: (tokens) => {
      session.getState().signIn(tokens);
      void navigate("/", { replace: true });
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        for (const [field, msg] of Object.entries(err.fieldErrors())) {
          if (field in form.getValues()) form.setError(field as keyof Values, { message: msg });
        }
      }
    },
  });
  const e = form.formState.errors;
  const options = useAuthOptions();

  if (options.data?.signup_enabled === false) {
    return (
      <AuthLayout
        title="Sign-up is closed"
        subtitle="New organizations are created by the administrator of this deployment. Ask your organization's owner for an invitation."
      >
        <Button asChild variant="primary" size="lg" className="w-full">
          <Link to="/sign-in">Go to sign in</Link>
        </Button>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="Create your organization"
      subtitle="You'll be the owner and can invite your team next."
      footer={
        <>
          Already have an account?{" "}
          <Link to="/sign-in" className="font-medium text-accent hover:underline">
            Sign in
          </Link>
        </>
      }
    >
      <form className="flex flex-col gap-4" noValidate onSubmit={form.handleSubmit((v) => signup.mutate(v))}>
        <FormError message={signup.error ? errorMessage(signup.error) : null} />
        <Field label="Your name" error={e.full_name?.message}>
          <Input autoComplete="name" autoFocus {...form.register("full_name")} />
        </Field>
        <Field label="Work email" error={e.email?.message}>
          <Input type="email" autoComplete="email" {...form.register("email")} />
        </Field>
        <Field label="Password" hint="At least 12 characters. A passphrase works well." error={e.password?.message}>
          <Input type="password" autoComplete="new-password" {...form.register("password")} />
        </Field>
        <Field label="Organization name" error={e.organization_name?.message}>
          <Input autoComplete="organization" {...form.register("organization_name")} />
        </Field>
        <Button type="submit" variant="primary" size="lg" className="mt-1 w-full" loading={signup.isPending}>
          Create organization
        </Button>
      </form>
    </AuthLayout>
  );
}
