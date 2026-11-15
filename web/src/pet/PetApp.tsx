import { useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";

import { chatLayout } from "./layout";
import { animationFor, dragAnimation, type PetState, type PetViewModel } from "./state";

type PetSnapshot = {
  connected: boolean;
  model: PetViewModel;
};

type PetController = {
  start(listener: (snapshot: PetSnapshot) => void): void;
  stop(): void;
  send(text: string): Promise<void>;
  requestBrowserToken(): Promise<string>;
};

type PetBridge = {
  setInteractive(active: boolean): void;
  openChat(): void;
  startDrag(x: number, y: number): void;
  moveDrag(x: number, y: number): void;
  endDrag(): void;
  openWorkbench(token: string): Promise<void>;
};

const disconnectedModel: PetViewModel = {
  sessionId: "",
  sessionTitle: "当前会话",
  activeRunId: null,
  state: "idle",
  reminder: null,
};

function reducedMotion(): boolean {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

type PetSpriteProps = {
  state: PetState;
  onContextMenu: (event: React.MouseEvent) => void;
  onPointerDown: (event: ReactPointerEvent<HTMLSpanElement>) => void;
  onPointerMove: (event: ReactPointerEvent<HTMLSpanElement>) => void;
  onPointerUp: (event: ReactPointerEvent<HTMLSpanElement>) => void;
  onPointerCancel: (event: ReactPointerEvent<HTMLSpanElement>) => void;
  onPointerEnter: () => void;
  onPointerLeave: () => void;
};

function PetSprite({ state, ...interactions }: PetSpriteProps) {
  const animation = animationFor(state);
  const [frame, setFrame] = useState(0);
  const isReducedMotion = reducedMotion();

  useEffect(() => {
    setFrame(0);
  }, [animation]);

  useEffect(() => {
    if (isReducedMotion || !animation.loop) return undefined;
    const timer = window.setTimeout(
      () => setFrame((value) => (value + 1) % animation.frameCount),
      animation.durations[frame] ?? animation.durations[0],
    );
    return () => window.clearTimeout(timer);
  }, [animation, frame, isReducedMotion]);

  return (
    <span
      {...interactions}
      className={`pet-sprite pet-sprite--row-${animation.row} pet-sprite--frame-${isReducedMotion ? 0 : frame}`}
      role="img"
      aria-label="法尔伽桌宠"
    />
  );
}

export function PetApp({ controller, bridge }: { controller: PetController; bridge: PetBridge }) {
  const [snapshot, setSnapshot] = useState<PetSnapshot>({ connected: false, model: disconnectedModel });
  const [greeting, setGreeting] = useState(true);
  const [sendError, setSendError] = useState<string | null>(null);
  const [dismissedReminderId, setDismissedReminderId] = useState<string | null>(null);
  const [dragState, setDragState] = useState<"running-left" | "running-right" | null>(null);
  const dragOrigin = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    controller.start(setSnapshot);
    return () => controller.stop();
  }, [controller]);

  useEffect(() => {
    const timer = window.setTimeout(() => setGreeting(false), 8_000);
    return () => window.clearTimeout(timer);
  }, []);

  const openChat = (event: React.MouseEvent) => {
    event.preventDefault();
    bridge.openChat();
  };

  const beginDrag = (event: ReactPointerEvent<HTMLSpanElement>) => {
    if (event.button !== 0) return;
    dragOrigin.current = { x: event.screenX, y: event.screenY };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    bridge.startDrag(event.screenX, event.screenY);
  };

  const moveDrag = (event: ReactPointerEvent<HTMLSpanElement>) => {
    if (!dragOrigin.current) return;
    bridge.moveDrag(event.screenX, event.screenY);
    setDragState(dragAnimation(event.screenX - dragOrigin.current.x));
  };

  const endDrag = (event: ReactPointerEvent<HTMLSpanElement>) => {
    if (!dragOrigin.current) return;
    dragOrigin.current = null;
    const sprite = event.currentTarget;
    if (sprite.hasPointerCapture?.(event.pointerId)) {
      sprite.releasePointerCapture?.(event.pointerId);
    }
    const remainsOverSprite = event.type === "pointerup" && sprite.matches(":hover");
    bridge.endDrag();
    setDragState(null);
    if (remainsOverSprite) bridge.setInteractive(true);
  };

  const openWorkbench = async () => {
    try {
      await bridge.openWorkbench(await controller.requestBrowserToken());
    } catch {
      setSendError("连接失败，请重新打开桌宠");
    }
  };

  const dismissReminder = (reminderId: string) => {
    bridge.setInteractive(false);
    setDismissedReminderId(reminderId);
  };

  const visibleReminder = snapshot.model.reminder?.id === dismissedReminderId
    ? null
    : snapshot.model.reminder;
  const visibleState = dragState ?? (greeting ? "waving" : snapshot.model.state);
  const layout = chatLayout("left");
  const layoutStyle = {
    "--pet-sprite-left": `${layout.spriteLeft}px`,
    "--pet-reminder-left": `${layout.reminderLeft}px`,
  } as CSSProperties;

  return (
    <main className="pet" style={layoutStyle}>
      <PetSprite
        state={visibleState}
        onContextMenu={openChat}
        onPointerDown={beginDrag}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onPointerEnter={() => bridge.setInteractive(true)}
        onPointerLeave={() => bridge.setInteractive(false)}
      />

      {sendError && <p className="pet-send-error" role="alert">{sendError}</p>}

      {visibleReminder && (
        <aside
          className="pet-reminder"
          role="status"
        >
          <span>{visibleReminder.text}</span>
          {visibleReminder.kind === "approval" && (
            <button
              type="button"
              onPointerEnter={() => bridge.setInteractive(true)}
              onPointerLeave={() => bridge.setInteractive(false)}
              onClick={() => void openWorkbench()}
            >
              打开工作台
            </button>
          )}
          <button
            type="button"
            aria-label="关闭提醒"
            onPointerEnter={() => bridge.setInteractive(true)}
            onPointerLeave={() => bridge.setInteractive(false)}
            onClick={() => dismissReminder(visibleReminder.id)}
          >
            ×
          </button>
        </aside>
      )}
    </main>
  );
}
