import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { app, BrowserWindow, ipcMain, screen, shell } from "electron";

import { chatWindowBounds, focusExistingChatWindow } from "./chatWindow.js";
import { setPetMouseInteractivity } from "./mouseEvents.js";
import { chatSideForBounds, clampWindowPosition, toIntegerPoint, type Point } from "./windowGeometry.js";

const WINDOW_SIZE = { width: 520, height: 320 };
const moduleDirectory = dirname(fileURLToPath(import.meta.url));

type DesktopEnvironment = { origin: string; token: string };
type DragState = { pointer: Point; windowPosition: Point };

let petWindow: BrowserWindow | null = null;
let chatWindow: BrowserWindow | null = null;
let dragState: DragState | null = null;

function readDesktopEnvironment(): DesktopEnvironment | null {
  const origin = process.env.SJTUCLAW_GATEWAY_ORIGIN;
  const token = process.env.SJTUCLAW_PET_BOOTSTRAP_TOKEN;
  if (!isGatewayOrigin(origin) || typeof token !== "string" || token.length < 43) return null;
  return { origin, token };
}

function isGatewayOrigin(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const match = /^http:\/\/127\.0\.0\.1:([1-9]\d*)$/.exec(value);
  if (!match) return false;
  const port = Number(match[1]);
  return Number.isInteger(port) && port >= 1 && port <= 65_535;
}

function isFiniteInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && Number.isInteger(value);
}

function positionFilePath(): string {
  return join(app.getPath("userData"), "varka-window.json");
}

function readSavedPosition(): Point | null {
  const path = positionFilePath();
  if (!existsSync(path)) return null;
  try {
    const value: unknown = JSON.parse(readFileSync(path, "utf8"));
    if (value === null || typeof value !== "object") return null;
    const { x, y } = value as Record<string, unknown>;
    return isFiniteInteger(x) && isFiniteInteger(y) ? { x, y } : null;
  } catch {
    return null;
  }
}

function saveWindowPosition(): void {
  if (!petWindow || petWindow.isDestroyed()) return;
  const { x, y } = petWindow.getBounds();
  try {
    writeFileSync(positionFilePath(), JSON.stringify({ x, y }), "utf8");
  } catch {
    // Persisting a convenience position must not disrupt the pet.
  }
}

function restoreWindowPosition(window: BrowserWindow): void {
  const saved = readSavedPosition();
  if (!saved) return;
  const workArea = screen.getDisplayNearestPoint(saved).workArea;
  const position = clampWindowPosition(saved, WINDOW_SIZE, workArea);
  window.setPosition(position.x, position.y);
}

async function createPetWindow(environment: DesktopEnvironment): Promise<BrowserWindow> {
  const window = new BrowserWindow({
    ...WINDOW_SIZE,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    skipTaskbar: true,
    hasShadow: false,
    backgroundColor: "#00000000",
    webPreferences: {
      preload: join(moduleDirectory, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  const petUrl = `${environment.origin}/pet.html#bootstrap=${encodeURIComponent(environment.token)}`;

  restoreWindowPosition(window);
  window.webContents.on("will-navigate", (event, url) => {
    if (url !== petUrl) event.preventDefault();
  });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  setPetMouseInteractivity(window, false);
  window.on("closed", () => {
    if (petWindow === window) petWindow = null;
    dragState = null;
  });
  await window.loadURL(petUrl);
  return window;
}

async function openChatWindow(environment: DesktopEnvironment): Promise<void> {
  if (focusExistingChatWindow(chatWindow) || !petWindow || petWindow.isDestroyed()) return;
  const petBounds = petWindow.getBounds();
  const bounds = chatWindowBounds(
    petBounds,
    screen.getDisplayMatching(petBounds).workArea,
  );
  const window = new BrowserWindow({
    ...bounds,
    show: false,
    resizable: false,
    autoHideMenuBar: true,
    backgroundColor: "#f4f7fb",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  const chatUrl = `${environment.origin}/pet-chat.html`;
  chatWindow = window;
  window.webContents.on("will-navigate", (event, url) => {
    if (url !== chatUrl) event.preventDefault();
  });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.once("ready-to-show", () => {
    if (chatWindow !== window || window.isDestroyed()) return;
    window.show();
    window.focus();
  });
  window.on("closed", () => {
    if (chatWindow === window) chatWindow = null;
  });
  try {
    await window.loadURL(chatUrl);
  } catch {
    if (!window.isDestroyed()) window.destroy();
    if (chatWindow === window) chatWindow = null;
    process.stderr.write("Unable to open SJTUClaw mini chat.\n");
  }
}

function isPetRenderer(
  event: { sender: Electron.WebContents; senderFrame: Electron.WebFrameMain | null },
  environment: DesktopEnvironment,
): boolean {
  if (!petWindow || petWindow.isDestroyed() || event.sender !== petWindow.webContents) return false;
  try {
    const frameUrl = new URL(event.senderFrame?.url ?? "");
    return frameUrl.origin === environment.origin && frameUrl.pathname === "/pet.html";
  } catch {
    return false;
  }
}

function registerIpcHandlers(environment: DesktopEnvironment): void {
  ipcMain.on("pet:open-chat", (event) => {
    if (!isPetRenderer(event, environment)) return;
    void openChatWindow(environment);
  });

  ipcMain.on("pet:set-interactive", (event, active: unknown) => {
    if (!isPetRenderer(event, environment) || typeof active !== "boolean" || !petWindow) return;
    setPetMouseInteractivity(petWindow, active);
  });

  ipcMain.on("pet:drag-start", (event, x: unknown, y: unknown) => {
    const pointer = toIntegerPoint(x, y);
    if (!isPetRenderer(event, environment) || !pointer || !petWindow) return;
    const bounds = petWindow.getBounds();
    dragState = { pointer, windowPosition: { x: bounds.x, y: bounds.y } };
    setPetMouseInteractivity(petWindow, true);
  });

  ipcMain.on("pet:drag-move", (event, x: unknown, y: unknown) => {
    const pointer = toIntegerPoint(x, y);
    if (!isPetRenderer(event, environment) || !pointer || !dragState || !petWindow) return;
    const workArea = screen.getDisplayNearestPoint(pointer).workArea;
    const position = clampWindowPosition({
      x: dragState.windowPosition.x + pointer.x - dragState.pointer.x,
      y: dragState.windowPosition.y + pointer.y - dragState.pointer.y,
    }, WINDOW_SIZE, workArea);
    petWindow.setPosition(position.x, position.y);
  });

  ipcMain.on("pet:drag-end", (event) => {
    if (!isPetRenderer(event, environment) || !petWindow) return;
    dragState = null;
    saveWindowPosition();
    setPetMouseInteractivity(petWindow, false);
  });

  ipcMain.handle("pet:get-chat-side", (event) => {
    if (!isPetRenderer(event, environment) || !petWindow) return "left";
    const bounds = petWindow.getBounds();
    return chatSideForBounds(bounds, screen.getDisplayMatching(bounds).workArea);
  });

  ipcMain.handle("pet:open-workbench", async (event, token: unknown) => {
    if (!isPetRenderer(event, environment) || typeof token !== "string" || token.length < 43) return;
    await shell.openExternal(`${environment.origin}/#bootstrap=${encodeURIComponent(token)}`);
  });
}

function start(): void {
  const environment = readDesktopEnvironment();
  if (!environment) {
    process.stderr.write("Invalid SJTUClaw desktop-pet environment.\n");
    app.exit(1);
    return;
  }

  void app.whenReady().then(async () => {
    registerIpcHandlers(environment);
    petWindow = await createPetWindow(environment);
    app.on("before-quit", saveWindowPosition);
  }).catch(() => {
    process.stderr.write("Unable to start SJTUClaw desktop pet.\n");
    app.exit(1);
  });
}

start();
