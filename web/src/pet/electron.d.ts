export {};

declare global {
  interface Window {
    sjtuPet: {
      setInteractive(active: boolean): void;
      openChat(): void;
      startDrag(x: number, y: number): void;
      moveDrag(x: number, y: number): void;
      endDrag(): void;
      getChatSide(): Promise<"left" | "right">;
      openWorkbench(token: string): Promise<void>;
    };
  }
}
