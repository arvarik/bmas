import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

function source(path: string): string {
  return readFileSync(resolve(process.cwd(), path), "utf8");
}

describe("accessibility shell contract", () => {
  it("defines visible focus and pointer target baselines", () => {
    const css = source("src/app/globals.css");
    expect(css).toContain("outline: 2px solid var(--border-focus) !important");
    expect(css).toContain("min-block-size: 36px");
    expect(css).toContain("min-block-size: 44px");
  });

  it("removes the closed drawer from interaction and the accessibility tree", () => {
    const shell = source("src/app/ClientShell.tsx");
    const sidebar = source("src/components/ui/TaskSidebar.tsx");
    expect(sidebar).toContain("aria-hidden={drawerMode && !mobileOpen ? true : undefined}");
    expect(sidebar).toContain("inert={drawerMode && !mobileOpen}");
    expect(shell).toMatch(/<button\s+type="button"\s+className="mobile-backdrop"/);
  });

  it("traps keyboard focus and restores the opening control", () => {
    const focusTrap = source("src/hooks/useFocusTrap.ts");
    expect(focusTrap).toContain('event.key !== "Tab"');
    expect(focusTrap).toContain("event.shiftKey");
    expect(focusTrap).toContain("returnTarget.focus()");
  });

  it("uses one main landmark", () => {
    const shell = source("src/app/ClientShell.tsx");
    const files = source("src/components/features/FilesWorkspace.tsx");
    expect(shell.match(/<main\b/g)).toHaveLength(1);
    expect(files).not.toMatch(/<main\b/);
  });
});
