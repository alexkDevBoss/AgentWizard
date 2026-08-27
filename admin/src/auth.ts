/**
 * Cognito hosted UI, authorization code flow with PKCE.
 *
 * The console never handles a password: it redirects to Cognito, receives an
 * ID token, and sends it as a bearer token. Tokens live in sessionStorage, so
 * closing the tab ends the session -- appropriate for a console that can
 * message real people.
 */
import { UserManager, WebStorageStateStore, type User } from "oidc-client-ts";
import type { AppConfig } from "./types";

let manager: UserManager | null = null;

export function initAuth(config: AppConfig): UserManager {
  if (manager) return manager;
  manager = new UserManager({
    authority: `https://cognito-idp.${config.region}.amazonaws.com/${config.userPoolId}`,
    client_id: config.clientId,
    redirect_uri: `${window.location.origin}/`,
    post_logout_redirect_uri: `${window.location.origin}/`,
    response_type: "code",
    scope: "openid email profile",
    // Cognito's discovery document omits the hosted-UI logout endpoint, so it
    // is supplied explicitly.
    metadataSeed: {
      end_session_endpoint: `${config.loginDomain}/logout?client_id=${config.clientId}&logout_uri=${encodeURIComponent(window.location.origin + "/")}`,
    },
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
    stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
    automaticSilentRenew: true,
  });
  return manager;
}

function required(): UserManager {
  if (!manager) throw new Error("initAuth has not run yet");
  return manager;
}

/** Resolve the signed-in user, completing a redirect callback if one is in flight. */
export async function resolveUser(): Promise<User | null> {
  const um = required();
  const params = new URLSearchParams(window.location.search);

  if (params.has("code") && params.has("state")) {
    const user = await um.signinRedirectCallback();
    // Strip the code from the address bar so a refresh does not replay it.
    window.history.replaceState({}, "", window.location.pathname);
    return user;
  }
  if (params.has("error")) {
    window.history.replaceState({}, "", window.location.pathname);
    throw new Error(params.get("error_description") || params.get("error")!);
  }

  const user = await um.getUser();
  return user && !user.expired ? user : null;
}

export const signIn = () => required().signinRedirect();
export const signOut = async () => {
  await required().removeUser();
  await required().signoutRedirect();
};
