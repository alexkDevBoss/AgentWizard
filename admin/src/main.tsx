/**
 * Boot.
 *
 * Configuration is fetched at runtime from /config.json rather than baked in
 * at build time, so the same bundle can be deployed to dev and prod and the
 * Cognito ids are never committed to the repo.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import type { AppConfig } from "./types";
import "./styles.css";

const root = createRoot(document.getElementById("root")!);

fetch("/config.json", { cache: "no-store" })
  .then((response) => {
    if (!response.ok) throw new Error(`config.json returned ${response.status}`);
    return response.json() as Promise<AppConfig>;
  })
  .then((config) => {
    root.render(
      <StrictMode>
        <App config={config} />
      </StrictMode>,
    );
  })
  .catch((error: Error) => {
    root.render(
      <div className="gate">
        <div className="gate__mark">Handler</div>
        <h1 className="gate__title">This console is not configured.</h1>
        <p className="gate__error">
          {error.message} — run <code>make admin-deploy</code>, which writes
          config.json from the deployed stack outputs.
        </p>
      </div>,
    );
  });
