export type CommandReceipt = {
  command: string;
  lines: string[];
  isError: boolean;
};

export type TransientCommandItem = {
  id: string;
  command: string;
  receipt: CommandReceipt | null;
};

type TransientCommandState = Record<string, readonly TransientCommandItem[]>;

export function newTransientCommand(id: string, command: string): TransientCommandItem {
  return { id, command, receipt: null };
}

export function completeTransientCommand(
  state: TransientCommandState,
  sourceSessionId: string,
  targetSessionId: string,
  commandId: string,
  receipt: CommandReceipt,
): Record<string, TransientCommandItem[]> {
  return updateTransientCommand(
    state,
    sourceSessionId,
    targetSessionId,
    commandId,
    receipt,
  );
}

export function failTransientCommand(
  state: TransientCommandState,
  sourceSessionId: string,
  commandId: string,
  command: string,
  error: string,
): Record<string, TransientCommandItem[]> {
  const item = (state[sourceSessionId] ?? []).find(({ id }) => id === commandId);
  if (!item || item.receipt !== null) {
    return copyState(state);
  }

  return completeTransientCommand(
    state,
    sourceSessionId,
    sourceSessionId,
    commandId,
    { command, lines: [error], isError: true },
  );
}

function updateTransientCommand(
  state: TransientCommandState,
  sourceSessionId: string,
  targetSessionId: string,
  commandId: string,
  receipt: CommandReceipt,
): Record<string, TransientCommandItem[]> {
  const sourceItems = state[sourceSessionId] ?? [];
  const item = sourceItems.find(({ id }) => id === commandId);
  const nextState = copyState(state);

  if (!item) {
    return nextState;
  }

  nextState[sourceSessionId] = sourceItems.filter(({ id }) => id !== commandId);
  nextState[targetSessionId] = [
    ...(nextState[targetSessionId] ?? []),
    { ...item, receipt: { ...receipt, lines: [...receipt.lines] } },
  ];
  return nextState;
}

function copyState(state: TransientCommandState): Record<string, TransientCommandItem[]> {
  return Object.fromEntries(
    Object.entries(state).map(([sessionId, items]) => [sessionId, [...items]]),
  );
}
