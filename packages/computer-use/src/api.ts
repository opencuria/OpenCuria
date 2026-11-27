import { DOCUMENTATION, type NativeReply } from './protocol.js';

type Native = (op: string, args?: Record<string, unknown>) => Promise<NativeReply>;
type Write = (value: unknown, name?: string) => void;
type Vec2 = [number, number];
type Pattern = string | RegExp;
type QueryObject = {
  role?: Pattern;
  name?: Pattern;
  text?: Pattern;
  value?: Pattern;
  description?: Pattern;
  context?: Pattern;
  states?: string[];
  id?: number;
  within?: Target;
  index?: number;
};
type Query = Pattern | QueryObject;
type Target = number | Vec2 | Query;
type ObservationOptions = { emit?: boolean; disableDiffing?: boolean; root?: Target; maxNodes?: number };
type ScreenshotOptions = { emit?: boolean; maxDimension?: number; region?: Target | [number, number, number, number] };
type Node = {
  id: number; parent: number | null; depth: number; folded?: boolean; role: string; name: string; value: string;
  description: string; context: string; states: string[]; actions: string[]; line: string;
};
type Tree = { source: string; nodes: Node[]; truncated?: boolean; notice?: string };
type WindowInfo = { id: number; app: string; title?: string; focused?: boolean; modal?: boolean; window_type?: string; transient_for?: number };
type WindowQuery = string | RegExp | { windowId?: number; app?: Pattern; title?: Pattern; type?: string; modal?: boolean };

const MAX_EMITTED_TREE = 48 * 1024;
const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const ROLE_ALIASES: Record<string, string[]> = {
  button: ['push button', 'button', 'toggle button', 'menu button'],
  textbox: ['entry', 'text', 'password text', 'spin button', 'spin box', 'search box', 'terminal'],
  input: ['entry', 'text', 'password text', 'spin button', 'combo box', 'search box'],
  field: ['entry', 'text', 'password text', 'spin button', 'combo box', 'search box'],
  checkbox: ['check box', 'check menu item', 'toggle button', 'switch'],
  radio: ['radio button', 'radio menu item'],
  combobox: ['combo box'],
  select: ['combo box', 'list box', 'list'],
  dropdown: ['combo box', 'menu button'],
  link: ['link'],
  menuitem: ['menu item', 'check menu item', 'radio menu item'],
  option: ['menu item', 'list item', 'option', 'radio menu item', 'check menu item'],
  listitem: ['list item'],
  tab: ['page tab'],
  heading: ['heading'],
  dialog: ['dialog', 'file chooser', 'color chooser', 'font chooser'],
  alert: ['alert', 'notification'],
  image: ['image', 'icon'],
  cell: ['table cell', 'cell', 'grid cell'],
  row: ['table row', 'row', 'list item', 'tree item'],
  table: ['table', 'tree table', 'grid'],
  text: ['text', 'static', 'label', 'paragraph', 'caption'],
  container: ['panel', 'filler', 'section', 'generic', 'group', 'unknown'],
  slider: ['slider'],
  spinbutton: ['spin button', 'spin box'],
  switch: ['switch', 'toggle button'],
};

function isVec2(value: unknown): value is Vec2 {
  return Array.isArray(value) && value.length === 2 && value.every((item) => typeof item === 'number' && Number.isFinite(item));
}

function isQuery(value: unknown): value is Query {
  return typeof value === 'string' || value instanceof RegExp || (typeof value === 'object' && value !== null && !Array.isArray(value));
}

function describe(value: unknown): string {
  if (value instanceof RegExp) return String(value);
  if (typeof value === 'string') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(describe).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.entries(value).filter(([, item]) => item !== undefined).map(([key, item]) => `${key}:${describe(item)}`).join(',')}}`;
  }
  return String(value);
}

function textMatches(pattern: Pattern | undefined, value: string): boolean {
  if (pattern === undefined) return true;
  if (pattern instanceof RegExp) {
    pattern.lastIndex = 0;
    return pattern.test(value);
  }
  return value.toLowerCase().includes(String(pattern).toLowerCase());
}

function roleMatches(pattern: Pattern | undefined, node: Node): boolean {
  if (pattern === undefined) return true;
  if (pattern instanceof RegExp) return textMatches(pattern, node.role);
  const wanted = pattern.toLowerCase().trim();
  const aliases = ROLE_ALIASES[wanted.replace(/[\s_-]/g, '')];
  if (aliases?.includes(node.role)) return true;
  return node.role === wanted;
}

function nodeMatches(node: Node, query: Query): boolean {
  if (typeof query === 'string' || query instanceof RegExp) return textMatches(query, node.line);
  if (query.id !== undefined && node.id !== query.id) return false;
  if (!roleMatches(query.role, node)) return false;
  if (!textMatches(query.name, node.name)) return false;
  if (!textMatches(query.value, node.value)) return false;
  if (!textMatches(query.description, node.description)) return false;
  if (!textMatches(query.context, node.context)) return false;
  if (query.text !== undefined && !textMatches(query.text, [node.name, node.value, node.description, node.context].join('\n'))) return false;
  for (const state of query.states ?? []) {
    const negated = state.startsWith('!');
    const name = (negated ? state.slice(1) : state).toLowerCase();
    if (node.states.includes(name) === negated) return false;
  }
  return true;
}

function exact(node: Node, query: Query): boolean {
  const pattern = typeof query === 'string' ? query : query instanceof RegExp ? undefined : query.name ?? query.text;
  return typeof pattern === 'string' && node.name.toLowerCase().trim() === pattern.toLowerCase().trim();
}

type Purpose = 'any' | 'act' | 'edit';
const EDITABLE_ROLES = new Set(['combo box', 'list box', 'slider', 'spin button', 'spin box', 'check box', 'radio button', 'toggle button', 'switch', 'page tab list']);

function suits(node: Node, purpose: Purpose): boolean {
  if (purpose === 'any') return true;
  if (purpose === 'edit') return node.states.includes('editable') || EDITABLE_ROLES.has(node.role);
  return !['text', 'static', 'label', 'paragraph', 'heading', 'caption', 'section', 'panel', 'generic', 'filler'].includes(node.role)
    || node.states.includes('focusable') || node.actions.some((action) => !['', 'doDefault', 'showContextMenu'].includes(action));
}

function element(node: Node) {
  const result: Record<string, unknown> = { id: node.id, role: node.role, name: node.name };
  if (node.value) result.value = node.value;
  if (node.description) result.description = node.description;
  if (node.context) result.context = node.context;
  result.states = node.states;
  if (node.actions.length) result.actions = node.actions.filter(Boolean);
  result.line = `${node.id} ${node.line}`;
  return result;
}

function render(tree: Tree): string {
  const lines = [`Accessibility source: ${tree.source}`];
  if (tree.notice) lines.push(`[${tree.notice}]`);
  for (const node of tree.nodes) if (!node.folded) lines.push(`${'\t'.repeat(node.depth)}${node.id} ${node.line}`);
  return lines.join('\n');
}

function limit(text: string): string {
  if (Buffer.byteLength(text) <= MAX_EMITTED_TREE) return text;
  const lines = text.split('\n');
  const kept: string[] = [];
  let size = 200;
  for (const line of lines) {
    size += Buffer.byteLength(line) + 1;
    if (size > MAX_EMITTED_TREE) break;
    kept.push(line);
  }
  kept.splice(1, 0, `[Showing ${kept.length - 1} of ${lines.length - 1} lines (output limit). Use app.find(query) or getAXState({ root }) for the rest.]`);
  return kept.join('\n');
}

type Baseline = Map<number, { line: string; parent: number | null }>;

function baseline(tree: Tree): Baseline {
  return new Map(tree.nodes.filter((node) => !node.folded).map((node) => [node.id, { line: node.line, parent: node.parent }]));
}

function diff(previous: Baseline, full: Tree): string {
  const tree = { ...full, nodes: full.nodes.filter((node) => !node.folded) };
  const current = new Map(tree.nodes.map((node) => [node.id, node]));
  const added = new Set(tree.nodes.filter((node) => !previous.has(node.id)).map((node) => node.id));
  const lines: string[] = [];
  for (const node of tree.nodes) {
    const before = previous.get(node.id);
    if (added.has(node.id)) {
      if (node.parent !== null && added.has(node.parent)) continue;
      const base = node.depth;
      lines.push(`+ ${node.id} ${node.line}${node.parent !== null ? ` (in ${node.parent})` : ''}`);
      const stack = new Set([node.id]);
      for (const child of tree.nodes.slice(tree.nodes.indexOf(node) + 1)) {
        if (child.parent === null || !stack.has(child.parent) || !added.has(child.id)) {
          if (child.depth <= base) break;
          continue;
        }
        stack.add(child.id);
        lines.push(`${'\t'.repeat(child.depth - base)}+ ${child.id} ${child.line}`);
      }
    } else if (before && (before.line !== node.line || before.parent !== node.parent)) {
      lines.push(`~ ${node.id} ${node.line}${before.parent !== node.parent ? ` (moved to ${node.parent})` : ''}`);
    }
  }
  const removed = [...previous.entries()].filter(([id]) => !current.has(id));
  const removedIds = new Set(removed.map(([id]) => id));
  for (const [id, entry] of removed) {
    if (entry.parent !== null && removedIds.has(entry.parent)) continue;
    let descendants = 0;
    const pending = [id];
    while (pending.length) {
      const parent = pending.pop();
      for (const [other, value] of removed) if (value.parent === parent) { descendants++; pending.push(other); }
    }
    lines.push(`- ${id} ${entry.line}${descendants ? ` (and ${descendants} descendants)` : ''}`);
  }
  const header = `Accessibility source: ${tree.source} — changes since the previous state (+ added, ~ changed, - removed; getAXState({ disableDiffing: true }) shows everything)`;
  if (!lines.length) return `Accessibility source: ${tree.source}\n[No changes since the previous state]`;
  return [header, ...(tree.notice ? [`[${tree.notice}]`] : []), ...lines].join('\n');
}

function windowMatches(window: WindowInfo, query: WindowQuery): boolean {
  if (typeof query === 'string' || query instanceof RegExp) {
    return textMatches(query, `${window.app}\n${window.title ?? ''}`);
  }
  if (query.windowId !== undefined && window.id !== query.windowId) return false;
  if (query.app !== undefined && !textMatches(query.app, window.app)) return false;
  if (query.title !== undefined && !textMatches(query.title, window.title ?? '')) return false;
  if (query.type !== undefined && window.window_type !== query.type) return false;
  if (query.modal !== undefined && !!window.modal !== query.modal) return false;
  return true;
}

function windowLine(window: WindowInfo): string {
  return `${window.id} ${window.app}${window.title ? ` “${window.title}”` : ''}${window.window_type && window.window_type !== 'normal' ? ` (${window.window_type})` : ''}`;
}

/** Adapt the public CUA contract to OpenCuria's run-owned native I/O service. */
export function createCua(native: Native, write: Write, emitImage: (value: Uint8Array) => Promise<void>, requestMeta: () => unknown = () => undefined) {
  let documented = false;
  let documentationRequest: unknown;
  function documentation() {
    if (!documented) {
      write(DOCUMENTATION, 'cua.core');
      documented = true;
      documentationRequest = requestMeta();
    }
  }
  async function inventory(op: string, options: ObservationOptions = {}) {
    documentation();
    const result = await native(op);
    const value = result.items ?? result;
    if (options.emit !== false) write(JSON.stringify(value), 'cua.state');
    return value;
  }

  async function windows(): Promise<WindowInfo[]> {
    return (await native('windows')).items as WindowInfo[];
  }

  async function resolveWindow(target: unknown, timeoutMs = 0): Promise<WindowInfo> {
    if (target && typeof target === 'object' && !(target instanceof RegExp) && 'windowId' in target) {
      const id = (target as { windowId: unknown }).windowId;
      if (!Number.isSafeInteger(id) || (id as number) <= 0) throw new Error('getApp requires { windowId } from listWindows(), an app name, or { app, title }.');
      const found = (await windows()).find((window) => window.id === id);
      if (!found) throw new Error(`Window ${id} is unavailable.`);
      return found;
    }
    if (!(typeof target === 'string' || target instanceof RegExp || (target && typeof target === 'object'))) {
      throw new Error('getApp requires an app name, a RegExp, { windowId }, or { app, title }.');
    }
    const query = target as WindowQuery;
    const deadline = Date.now() + timeoutMs;
    let launched = false;
    while (true) {
      const all = await windows();
      const matching = all.filter((window) => windowMatches(window, query));
      if (matching.length === 1) return matching[0];
      if (matching.length > 1) {
        throw new Error(`Several windows match ${describe(query)}; pass { windowId } or a title:\n${matching.slice(0, 12).map(windowLine).join('\n')}`);
      }
      if (!launched && (typeof query === 'string' || (typeof query === 'object' && !(query instanceof RegExp) && typeof query.app === 'string'))) {
        const name = String(typeof query === 'string' ? query : (query as { app: string }).app).toLowerCase();
        const apps = ((await native('apps')).items as Array<{ id: string; displayName: string; windows?: WindowInfo[] }>);
        const candidates = apps.filter((app) => app.displayName.toLowerCase() === name || app.id.toLowerCase().replace(/\.desktop$/, '') === name);
        const loose = candidates.length ? candidates : apps.filter((app) => app.displayName.toLowerCase().includes(name));
        // An installed app's own windows (their class can differ from its name).
        const running = (loose.length === 1 ? loose[0].windows ?? [] : []).filter((window) => all.some((open) => open.id === window.id));
        if (running.length === 1) return all.find((open) => open.id === running[0].id) as WindowInfo;
        if (running.length > 1) {
          throw new Error(`Several windows of ${loose[0].displayName} are open; pass { windowId } or a title:\n${running.slice(0, 12).map(windowLine).join('\n')}`);
        }
        if (loose.length > 1 && !all.some((window) => windowMatches(window, query))) {
          throw new Error(`Several installed apps match ${describe(query)}; pass the exact name:\n${loose.slice(0, 10).map((app) => `${app.displayName} (${app.id})`).join('\n')}`);
        }
        if (loose.length === 1) {
          await native('launch_app', { app: loose[0].id });
          launched = true;
          const launchDeadline = Date.now() + 20000;
          const before = new Set(all.map((window) => window.id));
          while (Date.now() < launchDeadline) {
            await sleep(250);
            const fresh = (await windows()).filter((window) => !before.has(window.id));
            const own = fresh.filter((window) => windowMatches(window, query) || window.app.toLowerCase().includes(name) || name.includes(window.app.toLowerCase()));
            if (own.length === 1) return own[0];
            if (fresh.length === 1 && Date.now() > launchDeadline - 15000) return fresh[0];
          }
          throw new Error(`Launched ${loose[0].displayName}, but no window appeared within 20 s.`);
        }
      }
      if (Date.now() >= deadline) {
        throw new Error(`No open window matches ${describe(query)}. Open windows:\n${all.slice(0, 20).map(windowLine).join('\n') || '(none)'}`);
      }
      await sleep(200);
    }
  }

  async function getApp(target: unknown, options: { emit?: boolean } = {}) {
    documentation();
    const window = await resolveWindow(target);
    const binding = await native('bind', { windowId: window.id });
    const base = () => ({ windowId: window.id, identity: binding.identity });
    const baselines = new Map<string, Baseline>();

    async function readTree(options: { root?: Target; maxNodes?: number; disableDiffing?: boolean } = {}): Promise<Tree> {
      const root = options.root === undefined ? undefined : await elementId(options.root);
      return await native('state', { ...base(), text: true, root, maxNodes: options.maxNodes, fresh: options.disableDiffing || undefined }) as Tree;
    }

    async function candidates(query: Query, fresh = false): Promise<{ tree: Tree; matches: Node[] }> {
      const within = typeof query === 'object' && !(query instanceof RegExp) ? query.within : undefined;
      const tree = await readTree({ root: within, disableDiffing: fresh });
      return { tree, matches: tree.nodes.filter((node) => nodeMatches(node, query)) };
    }

    /** Poll cheaply, but re-read everything about once a second: some toolkits miss change events. */
    function poller() {
      let lastFresh = Date.now();
      return () => {
        if (Date.now() - lastFresh < 1000) return false;
        lastFresh = Date.now();
        return true;
      };
    }

    async function pick(query: Query, timeoutMs = 5000, purpose: Purpose = 'any'): Promise<Node> {
      const deadline = Date.now() + timeoutMs;
      const fresh = poller();
      let first = true;
      while (true) {
        const { tree, matches } = await candidates(query, !first && fresh());
        first = false;
        const index = typeof query === 'object' && !(query instanceof RegExp) ? query.index : undefined;
        if (index !== undefined) {
          if (matches[index]) return matches[index];
        } else if (matches.length === 1) {
          return matches[0];
        } else if (matches.length > 1) {
          // Labels often repeat a control's name; actions mean the control.
          for (const narrowed of [matches.filter((node) => suits(node, purpose)), matches.filter((node) => suits(node, purpose) && exact(node, query)), matches.filter((node) => exact(node, query))]) {
            if (narrowed.length === 1) return narrowed[0];
          }
          throw new Error(`${matches.length} elements match ${describe(query)}; add role/name/states, or index (0-based):\n${matches.slice(0, 8).map((node) => `${node.id} ${node.line}`).join('\n')}`);
        }
        if (Date.now() >= deadline) {
          throw new Error(`No element matches ${describe(query)} after ${timeoutMs} ms${tree.truncated ? ' (the tree was partial; pass within/root)' : ''}. Use app.find() to inspect candidates.`);
        }
        await sleep(200);
      }
    }

    async function elementId(target: Target, timeoutMs?: number, purpose: Purpose = 'any'): Promise<number> {
      if (typeof target === 'number') {
        if (!Number.isSafeInteger(target)) throw new Error('Element IDs are integers from the accessibility state.');
        return target;
      }
      if (isVec2(target)) throw new Error('This method needs an element (ID or query), not coordinates.');
      if (!isQuery(target)) throw new Error('Pass an element ID, a query such as { role: "button", name: "Save" }, or [x, y].');
      return (await pick(target, timeoutMs, purpose)).id;
    }

    async function locate(target: Target, timeoutMs?: number, purpose: Purpose = 'act'): Promise<{ elementId: number } | { coordinates: Vec2 }> {
      if (isVec2(target)) return { coordinates: target };
      return { elementId: await elementId(target, timeoutMs, purpose) };
    }

    async function act<T>(target: Target, timeoutMs: number | undefined, run: (located: { elementId: number } | { coordinates: Vec2 }) => Promise<T>, purpose: Purpose = 'act'): Promise<T> {
      const located = await locate(target, timeoutMs, purpose);
      try {
        return await run(located);
      } catch (error) {
        if (isQuery(target) && /no longer exists|was replaced|unknown for this window/.test(String((error as Error).message))) {
          return await run(await locate(target, timeoutMs, purpose));
        }
        throw error;
      }
    }

    function emitState(key: string, tree: Tree, options: ObservationOptions) {
      const full = render(tree);
      if (options.emit === false) return full;
      const previous = baselines.get(key);
      let shown = full;
      if (previous && !options.disableDiffing) {
        const changes = diff(previous, tree);
        if (changes.length < full.length * 0.7) shown = changes;
      }
      write(limit(shown), 'cua.state');
      baselines.set(key, baseline(tree));
      return full;
    }

    async function screenshot(options: ScreenshotOptions, text: boolean, observation: ObservationOptions = {}) {
      let region: unknown = options.region;
      if (region !== undefined && !(Array.isArray(region) && region.length === 4)) region = await elementId(region as Target);
      const root = observation.root === undefined ? undefined : await elementId(observation.root);
      const result = await native('state', { ...base(), image: true, text, region, root, maxDimension: options.maxDimension, maxNodes: observation.maxNodes });
      const bytes = Buffer.from(result.image, 'base64');
      let state: string | undefined;
      if (text) state = emitState(describe(observation.root ?? null), result as Tree, { ...observation, emit: options.emit });
      if (options.emit !== false) {
        await emitImage(bytes);
        if (result.region || result.scale !== 1) {
          write(`[Screenshot ${result.width}×${result.height}${result.region ? ` of window region ${result.region.join(',')}` : ''}${result.scale !== 1 ? `, scale ${result.scale}` : ''}. Coordinates read from this image work directly with click/scroll/drag.]`, 'cua.state');
        }
      }
      return { state, screenshot: bytes };
    }

    const app = {
      windowId: window.id,
      window,
      getAXState: async (options: ObservationOptions = {}) => emitState(describe(options.root ?? null), await readTree(options), options),
      getScreenshot: async (options: ScreenshotOptions = {}) => (await screenshot(options, false)).screenshot,
      getAXStateAndScreenshot: async (options: ScreenshotOptions & ObservationOptions = {}) => {
        const result = await screenshot(options, true, options);
        return { state: result.state as string, screenshot: result.screenshot };
      },
      find: async (query: Query, options: { emit?: boolean; limit?: number } = {}) => {
        if (!isQuery(query)) throw new Error('find expects a string, RegExp or { role, name, text, value, states, within }.');
        const { tree, matches } = await candidates(query);
        if (options.emit !== false) {
          const shown = Math.max(1, options.limit ?? 30);
          write(`${matches.length
            ? [...matches.slice(0, shown).map((node) => `${node.id} ${node.line}`), ...(matches.length > shown ? [`… ${matches.length - shown} more (pass { limit } or a narrower query)`] : [])].join('\n')
            : `No element matches ${describe(query)}${tree.truncated ? ' (the tree was partial)' : ''}`}`, 'cua.state');
        }
        return matches.map(element);
      },
      waitFor: async (query: Query, options: { timeoutMs?: number; gone?: boolean; emit?: boolean } = {}) => {
        if (!isQuery(query)) throw new Error('waitFor expects a string, RegExp or { role, name, text, value, states, within }.');
        const timeout = options.timeoutMs ?? 10000;
        if (!Number.isFinite(timeout) || timeout < 0) throw new Error('timeoutMs must be a non-negative number.');
        const deadline = Date.now() + timeout;
        const fresh = poller();
        let first = true;
        while (true) {
          const { matches } = await candidates(query, !first && fresh());
          first = false;
          if (!options.gone && matches.length) {
            if (options.emit !== false) write(`${matches.slice(0, 5).map((node) => `${node.id} ${node.line}`).join('\n')}`, 'cua.state');
            return element(matches[0]);
          }
          if (options.gone && !matches.length) {
            if (options.emit !== false) write(`Gone: ${describe(query)}`, 'cua.state');
            return undefined;
          }
          if (Date.now() >= deadline) {
            throw new Error(options.gone
              ? `waitFor timed out after ${timeout} ms: ${describe(query)} is still present (${matches[0].id} ${matches[0].line})`
              : `waitFor timed out after ${timeout} ms: nothing matches ${describe(query)}. Use app.find() to inspect candidates.`);
          }
          await sleep(250);
        }
      },
      click: async (target: Target, options: { mouseButton?: string; clickCount?: number; durationMs?: number; key?: string; timeoutMs?: number } = {}) => {
        const { timeoutMs, ...click } = options;
        if (click.durationMs !== undefined && (!Number.isSafeInteger(click.durationMs) || click.durationMs < 0)) throw new Error('durationMs must be a non-negative safe integer.');
        await act(target, timeoutMs, (located) => native('click', { ...base(), ...located, ...click }));
      },
      hover: async (target: Target, options: { timeoutMs?: number } = {}) => {
        await act(target, options.timeoutMs, (located) => native('hover', { ...base(), ...located }));
      },
      focus: async (target: Target, options: { timeoutMs?: number } = {}) => {
        await act(target, options.timeoutMs, async (located) => {
          if ('coordinates' in located) await native('click', { ...base(), ...located });
          else await native('focus', { ...base(), ...located });
        });
      },
      drag: async (from: Target, to: Target, options: { timeoutMs?: number } = {}) => {
        const start = await locate(from, options.timeoutMs);
        const end = await locate(to, options.timeoutMs);
        await native('drag', {
          ...base(),
          ...('coordinates' in start ? { from: start.coordinates } : { fromElementId: start.elementId }),
          ...('coordinates' in end ? { to: end.coordinates } : { toElementId: end.elementId }),
        });
      },
      scroll: async (target: Target, direction: string, distance?: number | { pixels: number }) => {
        if (typeof distance === 'number') throw new Error('Linux scroll accepts { pixels }, not pages.');
        if (distance && (!Number.isFinite(distance.pixels) || distance.pixels <= 0)) throw new Error('pixels must be a positive finite number.');
        await act(target, undefined, (located) => native('scroll', { ...base(), ...located, direction, ...(distance ? { pixels: distance.pixels } : {}) }));
      },
      scrollIntoView: async (target: Target, options: { timeoutMs?: number } = {}) => {
        await act(target, options.timeoutMs, async (located) => {
          if ('coordinates' in located) throw new Error('scrollIntoView needs an element.');
          await native('scroll_into_view', { ...base(), ...located });
        });
      },
      selectText: async (target: Target, text: string, options: { prefix?: string; suffix?: string; selectionType?: string } = {}) => {
        await act(target, undefined, async (located) => {
          if ('coordinates' in located) throw new Error('selectText needs an element.');
          await native('select_text', { ...base(), ...located, text, ...options });
        });
      },
      setValue: async (target: Target, value: string | number | boolean, options: { timeoutMs?: number } = {}) => {
        return await act(target, options.timeoutMs, async (located) => {
          if ('coordinates' in located) throw new Error('setValue needs an element (ID or query), not coordinates.');
          return await native('set_value', { ...base(), ...located, value });
        }, 'edit');
      },
      paste: async (text: string, options?: { format?: string }) => {
        if (options?.format !== undefined && options.format !== 'text') throw new Error('Linux paste supports only text format.');
        await native('type_text', { ...base(), text });
      },
      pressKey: async (key: string, options: { durationMs?: number; repeat?: number } = {}) => {
        if (options.durationMs !== undefined && (!Number.isSafeInteger(options.durationMs) || options.durationMs < 0)) throw new Error('durationMs must be a non-negative safe integer.');
        await native('press_key', { ...base(), key, ...options });
      },
      typeCharacters: async (text: string) => {
        const result = await native('type_characters', { ...base(), text });
        return { charactersSent: result.charactersSent as number };
      },
      typeText: async (text: string, target?: Target, options: { timeoutMs?: number } = {}) => {
        if (target === undefined || target === null) {
          await native('type_text', { ...base(), text });
          return;
        }
        await act(target, options.timeoutMs, async (located) => {
          if ('coordinates' in located) {
            await native('click', { ...base(), ...located });
            await native('type_text', { ...base(), text });
          } else {
            await native('type_text', { ...base(), text, ...located });
          }
        }, 'edit');
      },
      performSecondaryAction: async (target: Target, action: string) => {
        await act(target, undefined, async (located) => {
          if ('coordinates' in located) throw new Error('performSecondaryAction needs an element.');
          await native('secondary', { ...base(), ...located, action });
        });
      },
      getBounds: async (target: Target) => await native('bounds', { ...base(), elementId: await elementId(target) }),
      chooseFiles: async (paths: string | string[], options: { trigger?: Target; timeoutMs?: number } = {}) => {
        let trigger: Record<string, unknown> = {};
        if (options.trigger !== undefined) {
          const located = await locate(options.trigger);
          trigger = 'coordinates' in located ? { triggerCoordinates: located.coordinates } : { triggerElementId: located.elementId };
        }
        const result = await native('choose_files', { ...base(), paths, timeoutMs: options.timeoutMs, ...trigger });
        write(`Chose ${result.files.length} file(s)${result.dialog ? ` in “${result.dialog}”` : ''}.`, 'cua.state');
        return { files: result.files as string[], dialog: result.dialog as string };
      },
      dropFiles: async (paths: string | string[], target: Target, options: { timeoutMs?: number } = {}) => {
        const located = await locate(target, options.timeoutMs);
        const result = await native('drop_files', { ...base(), paths, ...located });
        write(`Dropped ${result.files.length} file(s).`, 'cua.state');
        return { files: result.files as string[] };
      },
      activate: async () => { await native('activate', base()); },
    };
    const initial = await readTree();
    emitState('null', initial, { emit: options.emit });
    return app;
  }

  return {
    getState: (options?: ObservationOptions) => inventory('inventory', options),
    listWindows: (options?: ObservationOptions) => inventory('windows', options),
    listApps: (options?: ObservationOptions) => inventory('apps', options),
    getApp,
    waitForWindow: async (query: WindowQuery, options: { timeoutMs?: number; emit?: boolean } = {}) => {
      documentation();
      const timeout = options.timeoutMs ?? 10000;
      const deadline = Date.now() + timeout;
      while (true) {
        const matching = (await windows()).filter((window) => windowMatches(window, query));
        if (matching.length) {
          if (options.emit !== false) write(`${matching.slice(0, 5).map(windowLine).join('\n')}`, 'cua.state');
          return matching[0];
        }
        if (Date.now() >= deadline) throw new Error(`No window matching ${describe(query)} appeared within ${timeout} ms.`);
        await sleep(250);
      }
    },
    computer: {
      target: 'linux',
      launch_app: async (input: { app: string }) => { await native('launch_app', input); },
    },
    rewriteDocumentation: async () => {
      if (!documented) { documentation(); return; }
      const meta = requestMeta();
      if (meta == null || meta !== documentationRequest) {
        write(DOCUMENTATION, 'cua.core');
        documentationRequest = meta;
      }
    },
  };
}
