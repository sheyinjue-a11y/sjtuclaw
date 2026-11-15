import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("sjtuPet", {
  setInteractive: (active: boolean) => ipcRenderer.send("pet:set-interactive", active),
  openChat: () => ipcRenderer.send("pet:open-chat"),
  startDrag: (x: number, y: number) => ipcRenderer.send("pet:drag-start", x, y),
  moveDrag: (x: number, y: number) => ipcRenderer.send("pet:drag-move", x, y),
  endDrag: () => ipcRenderer.send("pet:drag-end"),
  getChatSide: () => ipcRenderer.invoke("pet:get-chat-side"),
  openWorkbench: (token: string) => ipcRenderer.invoke("pet:open-workbench", token),
});
