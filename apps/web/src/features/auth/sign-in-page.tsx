import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link, useNavigate, useSearchParams } from "react-router";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Field, FormError } from "@/components/ui/form";
import { Input } from "@/components/ui/primitives";
import { AuthLayout } from "@/features/auth/auth-layout";
import { api, errorMessage, unwrap } from "@/lib/api";
import { session } from "@/lib/session";
import { safeNext } from "@/lib/utils";

const schema = z.object({
  email: z.email("Enter a valid email address"),
  password: z.string().min(1, "Enter your password"),
});
type Values = z.infer<typeof schema>;

export default function SignInPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { email: "", password: "" } });
  const login = useMutation({
    mutationFn: (body: Values) => unwrap(api.POST("/api/v1/auth/login", { body })),
    onSuccess: (tokens) => {
      session.getState().signIn(tokens);
      void navigate(safeNext(params.get("next")), { replace: true });
    },
  });

  return (
    <AuthLayout
      title="Sign in"
      subtitle="Use the email your organization invited."
      footer={
        <>
          New here?{" "}
          <Link to="/sign-up" className="font-medium text-accent hover:underline">
            Create an organization
          </Link>
        </>
      }
    >
      <form className="flex flex-col gap-4" noValidate onSubmit={form.handleSubmit((v) => login.mutate(v))}>
        <FormError message={login.error ? errorMessage(login.error) : null} />
        <Field label="Email" error={form.formState.errors.email?.message}>
          <Input type="email" autoComplete="email" autoFocus {...form.register("email")} />
        </Field>
        <Field label="Password" error={form.formState.errors.password?.message}>
          <Input type="password" autoComplete="current-password" {...form.register("password")} />
        </Field>
        <Button type="submit" variant="primary" size="lg" className="mt-1 w-full" loading={login.isPending}>
          Sign in
        </Button>
      </form>
    </AuthLayout>
  );
}
