import { test, expect } from "@playwright/test";
import { projectTree } from "../src/codexTree.js";

test("canonical project assignment preserves independent tasks in the same directory", () => {
  const projects = [
    { id: "a", name: "Experiment", roots: [{ path: "/data/model" }] },
    { id: "empty", name: "Next experiment", roots: [] },
  ];
  const groups = projectTree(
    [
      { id: "assigned", cwd: "/worktrees/other", projectId: "a" },
      { id: "standalone", cwd: "/data/model", projectId: null },
    ],
    projects,
    "/home/user/.codex",
  );
  expect(
    groups.find((g) => g.projectId === "a").threads.map((t) => t.id),
  ).toEqual(["assigned"]);
  expect(
    groups.find((g) => g.key === "standalone").threads.map((t) => t.id),
  ).toEqual(["standalone"]);
  expect(groups.find((g) => g.projectId === "empty").threads).toEqual([]);
});

test("legacy histories distinguish same-named paths and keep home tasks independent", () => {
  const groups = projectTree(
    [
      { id: "a", cwd: "/data/model/" },
      { id: "b", cwd: "/other/model" },
      { id: "c", cwd: "/data/./model" },
      { id: "home", cwd: "/home/user" },
      { id: "temporary", cwd: "/tmp/deepqueue-smoke-001" },
      {
        id: "agent",
        cwd: "/var/lib/deepqueue/agents/1234567890abcdef1234567890abcdef",
      },
      { id: "scratch", cwd: "/home/user/.codex/visualizations/task" },
    ],
    [],
    "/home/user/.codex",
  );
  expect(
    groups.find((g) => g.key === "cwd:/data/model").threads.map((t) => t.id),
  ).toEqual(["a", "c"]);
  expect(
    groups.find((g) => g.key === "cwd:/other/model").threads.map((t) => t.id),
  ).toEqual(["b"]);
  expect(
    groups.find((g) => g.key === "standalone").threads.map((t) => t.id),
  ).toEqual(["home", "temporary", "agent", "scratch"]);
});

test("legacy root matching observes path boundaries and the closest project root", () => {
  const projects = [
    {
      id: "a",
      name: "Outer",
      roots: [
        { path: "/data/model" },
        { path: "/unrelated/longer/than/the/nested/root" },
      ],
    },
    { id: "b", name: "Inner", roots: [{ path: "/data/model/fold" }] },
  ];
  const groups = projectTree(
    [
      { id: "nested", cwd: "/data/model/fold/train" },
      { id: "sibling", cwd: "/data/models" },
    ],
    projects,
  );
  expect(groups.find((g) => g.projectId === "b").threads[0].id).toBe("nested");
  expect(groups.find((g) => g.projectId === "a").threads).toEqual([]);
  expect(groups.find((g) => g.key === "cwd:/data/models").threads[0].id).toBe(
    "sibling",
  );
});
