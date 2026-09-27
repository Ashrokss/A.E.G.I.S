// Build-time configuration (Vite inlines VITE_*). Both values come from
// `terraform output web_build_env`.
export const COGNITO_DOMAIN = (import.meta.env.VITE_COGNITO_DOMAIN as string | undefined)?.replace(/\/$/, "") ?? "";
export const COGNITO_CLIENT_ID = (import.meta.env.VITE_COGNITO_CLIENT_ID as string | undefined) ?? "";

export const authConfigError =
  !COGNITO_DOMAIN || !COGNITO_CLIENT_ID
    ? "Sign-in is not configured: set VITE_COGNITO_DOMAIN and VITE_COGNITO_CLIENT_ID."
    : !/^https:\/\//.test(COGNITO_DOMAIN)
      ? "VITE_COGNITO_DOMAIN must be the full hosted-UI URL (https://...)."
      : null;
