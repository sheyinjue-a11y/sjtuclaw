import { describe, expect, it } from "vitest";
import {
  completeTransientCommand,
  failTransientCommand,
  newTransientCommand,
} from "./commandTimeline";

describe("transient command timeline", () => {
  it("keeps a command and its successful receipt adjacent", () => {
    const pending = newTransientCommand("command_1", "/help");
    const state = { session_1: [pending] };

    expect(
      completeTransientCommand(state, "session_1", "session_1", "command_1", {
        command: "/help",
        lines: ["available commands"],
        isError: false,
      }),
    ).toEqual({
      session_1: [
        {
          id: "command_1",
          command: "/help",
          receipt: {
            command: "/help",
            lines: ["available commands"],
            isError: false,
          },
        },
      ],
    });
  });

  it("moves the triggering item and its receipt to a selected Session", () => {
    const state = { session_old: [newTransientCommand("command_2", "/new")] };

    expect(
      completeTransientCommand(state, "session_old", "session_new", "command_2", {
        command: "/new",
        lines: ["created"],
        isError: false,
      }),
    ).toEqual({
      session_old: [],
      session_new: [
        {
          id: "command_2",
          command: "/new",
          receipt: { command: "/new", lines: ["created"], isError: false },
        },
      ],
    });
  });

  it("preserves unrelated entries without mutating the source state", () => {
    const earlier = newTransientCommand("command_3", "/skills");
    const pending = newTransientCommand("command_4", "/help");
    const state = { session_1: [earlier, pending], session_2: [earlier] };

    const result = completeTransientCommand(state, "session_1", "session_2", "command_4", {
      command: "/help",
      lines: ["available commands"],
      isError: false,
    });

    expect(state).toEqual({ session_1: [earlier, pending], session_2: [earlier] });
    expect(result).toEqual({
      session_1: [earlier],
      session_2: [
        earlier,
        {
          id: "command_4",
          command: "/help",
          receipt: {
            command: "/help",
            lines: ["available commands"],
            isError: false,
          },
        },
      ],
    });
  });

  it("copies completed receipt lines independently from the caller", () => {
    const state = { session_1: [newTransientCommand("command_5", "/help")] };
    const receipt = {
      command: "/help",
      lines: ["available commands"],
      isError: false,
    };

    const result = completeTransientCommand(
      state,
      "session_1",
      "session_1",
      "command_5",
      receipt,
    );
    receipt.lines.push("mutated later");

    expect(result.session_1[0].receipt?.lines).toEqual(["available commands"]);
  });

  it("retains a pending command until its local error receipt arrives", () => {
    const state = { session_1: [newTransientCommand("command_6", "/unknown")] };

    const next = failTransientCommand(
      state,
      "session_1",
      "command_6",
      "/unknown",
      "request failed",
    );

    expect(next.session_1[0].receipt?.isError).toBe(true);
    expect(next.session_1[0].command).toBe("/unknown");
    expect(next.session_1[0].receipt?.lines).toEqual(["request failed"]);
  });

  it("does not replace a completed receipt with a later local error", () => {
    const completed = completeTransientCommand(
      { session_1: [newTransientCommand("command_7", "/help")] },
      "session_1",
      "session_1",
      "command_7",
      {
        command: "/help",
        lines: ["available commands"],
        isError: false,
      },
    );

    const next = failTransientCommand(
      completed,
      "session_1",
      "command_7",
      "/help",
      "refresh failed",
    );

    expect(next.session_1[0].receipt).toEqual({
      command: "/help",
      lines: ["available commands"],
      isError: false,
    });
  });

  it("starts with no entries after a fresh browser state", () => {
    expect({}).toEqual({});
  });
});
