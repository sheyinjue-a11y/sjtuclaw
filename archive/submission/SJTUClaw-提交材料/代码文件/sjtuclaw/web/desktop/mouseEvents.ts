type MouseEventWindow = {
  setIgnoreMouseEvents(ignore: boolean, options?: { forward: boolean }): void;
};

export function setPetMouseInteractivity(window: MouseEventWindow, active: boolean): void {
  if (active) {
    window.setIgnoreMouseEvents(false);
    return;
  }
  window.setIgnoreMouseEvents(true, { forward: true });
}
