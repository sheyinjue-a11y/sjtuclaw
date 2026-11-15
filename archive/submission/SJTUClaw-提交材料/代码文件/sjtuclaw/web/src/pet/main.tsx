import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { PetGatewayClient } from "./api";
import { PetApp } from "./PetApp";
import { PetSessionController, type PetEventSource } from "./controller";
import "./styles.css";

const root = createRoot(document.getElementById("pet-root")!);
const bootstrapToken = new URLSearchParams(window.location.hash.replace(/^#/, "")).get("bootstrap");

async function start(): Promise<void> {
  if (!bootstrapToken) {
    root.render(<div className="pet-startup-error" role="alert">连接失败，请重新打开桌宠</div>);
    return;
  }

  try {
    const client = new PetGatewayClient(window.location.origin, window.fetch.bind(window));
    await client.exchangeBootstrapFragment(window.location.hash);
    const controller = new PetSessionController({
      client,
      eventSource: (url) => new EventSource(url) as unknown as PetEventSource,
    });
    root.render(
      <StrictMode>
        <PetApp controller={controller} bridge={window.sjtuPet} />
      </StrictMode>,
    );
  } catch {
    root.render(<div className="pet-startup-error" role="alert">连接失败，请重新打开桌宠</div>);
  }
}

void start();
