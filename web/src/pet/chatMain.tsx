import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { PetGatewayClient } from "./api";
import { ChatApp } from "./ChatApp";
import { ChatSessionController } from "./chatController";
import "./chatStyles.css";

const client = new PetGatewayClient(window.location.origin, window.fetch.bind(window));
const controller = new ChatSessionController({ client });

createRoot(document.getElementById("pet-chat-root")!).render(
  <StrictMode>
    <ChatApp controller={controller} />
  </StrictMode>,
);
