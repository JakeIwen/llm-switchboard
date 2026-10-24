// Structural rules for a React app's source tree: folder import contracts,
// no import cycles, single-consumer components colocated with their consumer,
// and one component per file. Settings come from the `structure` block of
// .codeshape/config.json; the defaults are lionfeather-dev's layout.
import { existsSync, readFileSync } from 'node:fs'
import { dirname, extname, join, posix, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  configBlock,
  readCodeshapeConfig,
  runCheckCli,
  sortViolations,
} from './check-cli.mjs'
import { componentExports } from './component-exports.mjs'
import { scanScript } from './script-scanner.mjs'
import { relativePath, sourceFiles } from './source-files.mjs'

export const STRUCTURE_DEFAULTS = {
  sourceRoot: 'src',
  scriptExtensions: ['.ts', '.tsx', '.js', '.jsx', '.mjs'],
  // Files that may define components (and are checked for them).
  componentExtensions: ['.tsx', '.jsx'],
  // Folders are relative to sourceRoot. `from: '*'` is every module outside
  // the `to` folders; `allowDynamic` permits `import()` (dev-only loaders).
  contracts: [
    { from: 'content', to: ['components', 'pages'] },
    { from: 'components', to: ['pages'] },
    { from: '*', to: ['dev'], allowDynamic: true },
  ],
  // Export names never counted as components: framework route-module exports
  // such as React Router's ["Layout", "ErrorBoundary", "HydrateFallback"].
  ignoredExports: [],
  // Non-relative specifier prefixes mapped to repo-relative folders,
  // e.g. { "~/": "app/" }.
  aliases: {},
  // Path prefixes, relative to the repo root, to skip.
  exclude: [],
}
const STATIC_KINDS = new Set(['static', 'side-effect', 'require'])

function resolveCandidates(base) {
  const stem = base.replace(/\.(?:js|jsx|mjs)$/, '')
  const extensions = ['.ts', '.tsx', '.js', '.jsx', '.mjs']
  return [
    base,
    ...extensions.map((ext) => stem + ext),
    ...extensions.map((ext) => `${base}/index${ext}`),
  ]
}

function resolveImport(importer, specifier, settings, modules) {
  const clean = specifier.split(/[?#]/, 1)[0]
  let base = null
  if (/^\.\.?\//.test(clean)) {
    base = posix.join(posix.dirname(importer), clean)
  } else {
    const alias = Object.keys(settings.aliases).find((prefix) =>
      clean.startsWith(prefix),
    )
    if (!alias) return null
    base = posix.join(settings.aliases[alias], clean.slice(alias.length))
  }
  return resolveCandidates(base).find((file) => modules.has(file)) ?? null
}

function readModules(root, settings) {
  const paths = sourceFiles(
    root,
    [settings.sourceRoot],
    settings.scriptExtensions,
    settings.exclude,
  )
  return new Map(
    paths.map((path) => {
      const file = relativePath(root, path)
      const text = readFileSync(path, 'utf8')
      const jsx = settings.componentExtensions.includes(extname(path))
      const scan = scanScript(text, { jsx })
      return [file, { file, jsx, scan, text }]
    }),
  )
}

function linkImports(modules, settings) {
  for (const module of modules.values()) {
    module.edges = module.scan.imports.flatMap((item) => {
      const target = resolveImport(module.file, item.specifier, settings, modules)
      if (!target) return []
      return [{ ...item, target, exempt: module.scan.markerLines.has(item.line) }]
    })
  }
}

function folderOf(file, settings) {
  const inside = file.slice(settings.sourceRoot.length + 1)
  return inside.includes('/') ? inside.split('/', 1)[0] : ''
}

function contractViolations(modules, settings) {
  const violations = []
  for (const module of modules.values()) {
    const from = folderOf(module.file, settings)
    for (const contract of settings.contracts) {
      const applies =
        contract.from === '*' ? !contract.to.includes(from) : contract.from === from
      if (!applies) continue
      for (const edge of module.edges) {
        const to = folderOf(edge.target, settings)
        if (!contract.to.includes(to) || edge.exempt) continue
        if (contract.allowDynamic && edge.kind === 'dynamic') continue
        const source = contract.from === '*' ? 'the app' : `${contract.from}/`
        violations.push({
          file: module.file,
          line: edge.line,
          rule: 'contract',
          message: `${source} must not import ${to}/ (${edge.target})${contract.allowDynamic ? ' except through import()' : ''}.`,
        })
      }
    }
  }
  return violations
}

// Tarjan's strongly connected components over static, value-level imports.
function importCycles(modules) {
  const indexes = new Map()
  const lowLinks = new Map()
  const stack = []
  const onStack = new Set()
  const cycles = []
  let counter = 0
  const targets = (module) =>
    module.edges
      .filter((edge) => STATIC_KINDS.has(edge.kind) && !edge.typeOnly)
      .map((edge) => edge.target)
  const visit = (file) => {
    indexes.set(file, counter)
    lowLinks.set(file, counter)
    counter += 1
    stack.push(file)
    onStack.add(file)
    for (const target of targets(modules.get(file))) {
      if (!indexes.has(target)) {
        visit(target)
        lowLinks.set(file, Math.min(lowLinks.get(file), lowLinks.get(target)))
      } else if (onStack.has(target)) {
        lowLinks.set(file, Math.min(lowLinks.get(file), indexes.get(target)))
      }
    }
    if (lowLinks.get(file) !== indexes.get(file)) return
    const members = []
    let member
    do {
      member = stack.pop()
      onStack.delete(member)
      members.push(member)
    } while (member !== file)
    const selfLoop = targets(modules.get(file)).includes(file)
    if (members.length > 1 || selfLoop) cycles.push(members.sort())
  }
  for (const file of [...modules.keys()].sort()) {
    if (!indexes.has(file)) visit(file)
  }
  return cycles
}

function cycleViolations(modules) {
  return importCycles(modules).flatMap((members) => {
    const [first] = members
    const edge = modules
      .get(first)
      .edges.find((item) => members.includes(item.target) && !item.typeOnly)
    if (edge?.exempt) return []
    return [
      {
        file: first,
        line: edge?.line ?? 1,
        rule: 'cycle',
        message: `Import cycle among ${members.join(', ')}.`,
      },
    ]
  })
}

// Value importers of each module. Modules loaded through import() anywhere
// are code-split boundaries (a lazy view or dialog registry), like routes,
// so they get no entry and the colocation rule skips them.
function importersOf(modules) {
  const importers = new Map()
  const lazy = new Set()
  for (const module of modules.values()) {
    for (const edge of module.edges) {
      if (edge.kind === 'dynamic') lazy.add(edge.target)
    }
  }
  for (const module of modules.values()) {
    for (const edge of module.edges) {
      if (edge.typeOnly || edge.target === module.file || lazy.has(edge.target)) {
        continue
      }
      const set = importers.get(edge.target) ?? new Set()
      set.add(module.file)
      importers.set(edge.target, set)
    }
  }
  return importers
}

function componentViolations(modules, settings) {
  const importers = importersOf(modules)
  const violations = []
  for (const module of modules.values()) {
    if (!module.jsx) continue
    const components = componentExports(module.text, module.scan).filter(
      (item) =>
        !module.scan.markerLines.has(item.line) &&
        !settings.ignoredExports.includes(item.name),
    )
    if (components.length > 1) {
      const [, second] = components
      violations.push({
        file: module.file,
        line: second.line,
        rule: 'one-component',
        message: `Exports ${components.length} components (${components.map((item) => item.name).join(', ')}); give ${second.name} its own file.`,
      })
    }
    const users = [...(importers.get(module.file) ?? [])]
    if (!components.length || users.length !== 1) continue
    const userFolder = posix.dirname(users[0])
    const folder = posix.dirname(module.file)
    if (folder === userFolder || folder.startsWith(`${userFolder}/`)) continue
    violations.push({
      file: module.file,
      line: components[0].line,
      rule: 'colocate-component',
      message: `Only ${users[0]} imports this component; move it into ${userFolder}/ or a folder beneath it.`,
    })
  }
  return violations
}

export function checkStructure({ root, config }) {
  const absoluteRoot = resolve(root)
  const rawConfig = config ?? readCodeshapeConfig(absoluteRoot)
  const settings = configBlock(rawConfig, 'structure', STRUCTURE_DEFAULTS)
  if (!existsSync(join(absoluteRoot, settings.sourceRoot))) {
    throw new Error(`Source root not found: ${settings.sourceRoot}`)
  }
  const modules = readModules(absoluteRoot, settings)
  linkImports(modules, settings)
  return sortViolations([
    ...contractViolations(modules, settings),
    ...cycleViolations(modules),
    ...componentViolations(modules, settings),
  ])
}

const modulePath = fileURLToPath(import.meta.url)
if (process.argv[1] && resolve(process.argv[1]) === modulePath) {
  runCheckCli({
    argv: process.argv.slice(2),
    check: checkStructure,
    defaultRoot: resolve(dirname(modulePath), '..'),
    label: 'Structure check',
  })
}
