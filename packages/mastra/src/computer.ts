/**
 * Mastra's computer-use capability over a Groundbox agent computer (a sandbox started with
 * `desktop`): a Linux desktop with Chrome, driven through Groundbox's computer API.
 */
import type { ComputerActionInput } from '@groundbox/sdk';
import type { ComputerPosition, ComputerScreenSize, ComputerScreenshot, SandboxComputer } from '@mastra/core/workspace';

import type { GroundboxSandbox } from './sandbox.js';

/** Browser-style key names to the X keysym names the desktop takes. */
const KEY_NAMES: Record<string, string> = {
  enter: 'Return',
  return: 'Return',
  escape: 'Escape',
  esc: 'Escape',
  backspace: 'BackSpace',
  tab: 'Tab',
  space: 'space',
  ' ': 'space',
  delete: 'Delete',
  del: 'Delete',
  insert: 'Insert',
  home: 'Home',
  end: 'End',
  pageup: 'Page_Up',
  pagedown: 'Page_Down',
  arrowup: 'Up',
  arrowdown: 'Down',
  arrowleft: 'Left',
  arrowright: 'Right',
  up: 'Up',
  down: 'Down',
  left: 'Left',
  right: 'Right',
  control: 'ctrl',
  ctrl: 'ctrl',
  shift: 'shift',
  alt: 'alt',
  option: 'alt',
  meta: 'super',
  cmd: 'super',
  command: 'super',
  super: 'super',
  win: 'super',
  capslock: 'Caps_Lock',
};

/** One key name as the desktop takes it: `Enter` is `Return`, `ArrowUp` is `Up`, F-keys and letters pass through. */
export function toKeysym(key: string): string {
  const mapped = KEY_NAMES[key.toLowerCase()];
  if (mapped) return mapped;
  if (/^f\d{1,2}$/i.test(key)) return key.toUpperCase();
  return key;
}

/** A key or a chord (`['ctrl', 's']`) as one `key` action: `ctrl+s`. */
export function toKeyCombo(key: string | string[]): string {
  const keys = Array.isArray(key) ? key : [key];
  return keys.map(toKeysym).join('+');
}

export function createGroundboxComputer(sandbox: GroundboxSandbox): SandboxComputer {
  const act = async (action: ComputerActionInput) =>
    sandbox.retryOnDead(async () => {
      await sandbox.ensureRunning();
      return sandbox.groundbox.computer(sandbox.requireSandboxId(), action);
    });
  return {
    async screenshot(): Promise<ComputerScreenshot> {
      const shot = await sandbox.retryOnDead(async () => {
        await sandbox.ensureRunning();
        return sandbox.groundbox.computer_screenshot(sandbox.requireSandboxId(), { format: 'png' });
      });
      return { data: new Uint8Array(Buffer.from(shot.data, 'base64')), mediaType: 'image/png' };
    },
    async leftClick(x, y) {
      await act({ action: 'left_click', coordinate: [x, y] });
    },
    async rightClick(x, y) {
      await act({ action: 'right_click', coordinate: [x, y] });
    },
    async doubleClick(x, y) {
      await act({ action: 'double_click', coordinate: [x, y] });
    },
    async moveMouse(x, y) {
      await act({ action: 'mouse_move', coordinate: [x, y] });
    },
    async drag(from: ComputerPosition, to: ComputerPosition) {
      await act({ action: 'left_click_drag', start_coordinate: [from.x, from.y], coordinate: [to.x, to.y] });
    },
    async scroll(direction, amount) {
      await act({ action: 'scroll', scroll_direction: direction, scroll_amount: Math.max(1, Math.round(amount)) });
    },
    async type(text) {
      await act({ action: 'type', text });
    },
    async press(key) {
      await act({ action: 'key', text: toKeyCombo(key) });
    },
    async getScreenSize(): Promise<ComputerScreenSize> {
      const info = await sandbox.refresh();
      if (info?.desktop) return { width: info.desktop.width, height: info.desktop.height };
      const shot = await sandbox.groundbox.computer_screenshot(sandbox.requireSandboxId(), { format: 'jpeg', max_width: 64 });
      return { width: shot.screen.width, height: shot.screen.height };
    },
    async getCursorPosition(): Promise<ComputerPosition> {
      const response = await act({ action: 'cursor_position' });
      if (!response.cursor) throw new Error('Groundbox did not return a cursor position');
      return { x: response.cursor.x, y: response.cursor.y };
    },
    async streamUrl(): Promise<string | null> {
      try {
        const link = await sandbox.retryOnDead(async () => {
          await sandbox.ensureRunning();
          return sandbox.groundbox.create_desktop_link(sandbox.requireSandboxId(), { control: false });
        });
        return link.url;
      } catch {
        return null;
      }
    },
  };
}
