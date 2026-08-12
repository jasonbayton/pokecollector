/**
 * A hook harness for this repository's DOM-less test environment.
 *
 * There is no jsdom here, so the established style is renderToStaticMarkup plus
 * vi.mock (see ScanAddModal.test.js). That style cannot advance state: a server
 * render drops every setState and never runs an effect, so "tap the shutter
 * twice and see two photos staged" is unreachable with it.
 *
 * This harness closes that gap without touching production code. A test mocks
 * the `react` module's hook exports with `harness.hooks`, then calls the
 * component function directly. Hook cells persist between calls exactly as they
 * do for a mounted component, so setState followed by a re-render behaves the
 * way it does in a browser, and effects can be flushed and torn down on demand.
 *
 * The single assumption is React's own Rules of Hooks: the same hooks are
 * called in the same order on every render. A component that breaks that rule
 * is already broken in a browser.
 */

function sameDeps(previous, next) {
  if (!Array.isArray(previous) || !Array.isArray(next)) return false
  if (previous.length !== next.length) return false
  return previous.every((value, index) => Object.is(value, next[index]))
}

export function createHookHarness() {
  let cells = []
  let cursor = 0
  let pendingEffects = []
  let idCounter = 0

  const nextCell = create => {
    const index = cursor
    cursor += 1
    if (cells[index] === undefined) cells[index] = create()
    return cells[index]
  }

  const registerEffect = (effect, deps) => {
    const cell = nextCell(() => ({ primed: false, deps: undefined, cleanup: undefined, effect: null }))
    // deps === undefined means "every render", matching React.
    if (!cell.primed || deps === undefined || !sameDeps(cell.deps, deps)) {
      cell.primed = true
      cell.deps = deps
      cell.effect = effect
      pendingEffects.push(cell)
    }
  }

  const useMemo = (factory, deps) => {
    const cell = nextCell(() => ({ primed: false, deps: undefined, value: undefined }))
    if (!cell.primed || deps === undefined || !sameDeps(cell.deps, deps)) {
      cell.primed = true
      cell.deps = deps
      cell.value = factory()
    }
    return cell.value
  }

  const hooks = {
    useState(initial) {
      const cell = nextCell(() => ({ value: typeof initial === 'function' ? initial() : initial }))
      const setValue = next => {
        cell.value = typeof next === 'function' ? next(cell.value) : next
      }
      return [cell.value, setValue]
    },
    useReducer(reducer, initialArg, init) {
      const cell = nextCell(() => ({ value: init ? init(initialArg) : initialArg }))
      return [cell.value, action => { cell.value = reducer(cell.value, action) }]
    },
    useRef(initial) {
      return nextCell(() => ({ current: initial }))
    },
    useMemo,
    useCallback: (callback, deps) => useMemo(() => callback, deps),
    useId: () => nextCell(() => {
      idCounter += 1
      return { id: `harness-id-${idCounter}` }
    }).id,
    useEffect: registerEffect,
    useLayoutEffect: registerEffect,
  }

  /** Renders once. Effects are queued, not run - call flushEffects for those. */
  const render = (Component, props = {}) => {
    cursor = 0
    pendingEffects = []
    return Component(props)
  }

  const flushEffects = () => {
    const queue = pendingEffects
    pendingEffects = []
    for (const cell of queue) {
      if (typeof cell.cleanup === 'function') cell.cleanup()
      const cleanup = cell.effect()
      cell.cleanup = typeof cleanup === 'function' ? cleanup : undefined
    }
  }

  const renderAndFlush = (Component, props = {}) => {
    const tree = render(Component, props)
    flushEffects()
    return tree
  }

  /** Runs every live cleanup, in cell order, as an unmount does. */
  const unmount = () => {
    for (const cell of cells) {
      if (cell && typeof cell.cleanup === 'function') {
        cell.cleanup()
        cell.cleanup = undefined
      }
    }
    pendingEffects = []
  }

  const reset = () => {
    cells = []
    cursor = 0
    pendingEffects = []
    idCounter = 0
  }

  return { flushEffects, hooks, render, renderAndFlush, reset, unmount }
}

/**
 * One instance per test file. A vi.mock('react') factory and the test body both
 * import this module, and ESM hands them the same object.
 */
export const hookHarness = createHookHarness()

/** Depth-first walk of a React element tree, including arrays of children. */
export function* walkTree(node) {
  if (Array.isArray(node)) {
    for (const child of node) yield* walkTree(child)
    return
  }
  if (!node || typeof node !== 'object') return
  yield node
  yield* walkTree(node.props?.children)
}

export function findAll(tree, predicate) {
  return [...walkTree(tree)].filter(predicate)
}

export function findOne(tree, predicate) {
  const matches = findAll(tree, predicate)
  if (matches.length !== 1) {
    throw new Error(`Expected exactly one match in the tree, found ${matches.length}.`)
  }
  return matches[0]
}
