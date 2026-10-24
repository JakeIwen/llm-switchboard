// Stylesheet rules for a React app: palette-only colors and theme selectors,
// flat selectors, no duplicates or unused classes, a size limit, and the
// ownership rules in css-ownership.mjs. Paths come from the `css` block of
// .codeshape/config.json; the defaults are lionfeather-dev's layout.
import { existsSync, readFileSync } from 'node:fs'
import { dirname, extname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  configBlock,
  readCodeshapeConfig,
  runCheckCli,
  sortViolations,
} from './check-cli.mjs'
import { checkCssOwnership } from './css-ownership.mjs'
import {
  addMarkerLines,
  consumeQuoted,
  lineLocator,
  scanScript,
} from './script-scanner.mjs'
import { relativePath, sourceFiles } from './source-files.mjs'

export const CSS_DEFAULTS = {
  // Folders (relative to the repo root) holding the app's scripts and styles.
  sourceRoots: ['src'],
  // The one stylesheet allowed to hold color literals and theme selectors.
  palette: 'src/styles/palette.css',
  // Prefix of the global style layer; its classes may be used anywhere.
  globalStyles: 'src/styles/',
  // HTML files whose class attributes and words count as references.
  html: ['index.html'],
  // Files whose string literals are class references (JSX `className` or
  // Preact `class`); other script files count as data.
  jsxExtensions: ['.tsx', '.jsx'],
  scriptExtensions: ['.ts', '.tsx', '.js', '.jsx', '.mjs'],
  // Attribute selectors that may appear only in the palette.
  themeAttributes: ['data-theme', 'data-palette'],
  // Path prefixes, relative to the repo root, to skip.
  exclude: [],
}
const DEFAULT_FILE_LINES = 800
const NAMED_COLORS = new Set(
  `aliceblue antiquewhite aqua aquamarine azure beige bisque black
  blanchedalmond blue blueviolet brown burlywood cadetblue chartreuse
  chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan
  darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta
  darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
  darkslateblue darkslategray darkslategrey darkturquoise darkviolet
  deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite
  forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green
  greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender
  lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
  lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon
  lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue
  lightyellow lime limegreen linen magenta maroon mediumaquamarine mediumblue
  mediumorchid mediumpurple mediumseagreen mediumslateblue mediumspringgreen
  mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin
  navajowhite navy oldlace olive olivedrab orange orangered orchid
  palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff
  peru pink plum powderblue purple rebeccapurple red rosybrown royalblue
  saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue
  slateblue slategray slategrey snow springgreen steelblue tan teal thistle
  tomato turquoise violet wheat white whitesmoke yellow yellowgreen`
    .split(/\s+/)
    .filter(Boolean),
)
// At-rules whose condition separates otherwise identical selectors.
const CONTEXT_AT_RULES = new Set(['media', 'supports', 'container', 'layer', 'scope'])
const DECLARATION_AT_RULES = new Set([
  'counter-style',
  'font-face',
  'font-feature-values',
  'page',
  'property',
])

function isCssUrlStart(text, index) {
  const previous = index > 0 ? text[index - 1] : ''
  return (
    text.slice(index, index + 4).toLowerCase() === 'url(' &&
    !/[A-Za-z0-9_-]/.test(previous)
  )
}

function consumeCssUrl(text, start) {
  for (let index = start + 4; index < text.length; index += 1) {
    if (text[index] === '\\') index += 1
    else if (text[index] === '"' || text[index] === "'")
      index = consumeQuoted(text, index) - 1
    else if (text[index] === ')') return index + 1
  }
  return text.length
}

function stripCssComments(text) {
  const output = [...text]
  const lineAt = lineLocator(text)
  const markerLines = new Set()
  let hasSizeExemption = false
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === '"' || text[index] === "'") {
      index = consumeQuoted(text, index) - 1
      continue
    }
    if (isCssUrlStart(text, index)) {
      index = consumeCssUrl(text, index) - 1
      continue
    }
    if (text[index] !== '/' || text[index + 1] !== '*') continue
    const close = text.indexOf('*/', index + 2)
    const end = close === -1 ? text.length : close + 2
    const comment = text.slice(index, end)
    const commentBody = text.slice(index + 2, close === -1 ? end : close)
    addMarkerLines(comment, index, lineAt, markerLines)
    if (/codeshape:\s*allow-size[ \t]+\S[^\r\n]*/.test(commentBody)) {
      hasSizeExemption = true
    }
    for (let cursor = index; cursor < end; cursor += 1) {
      if (output[cursor] !== '\n' && output[cursor] !== '\r') {
        output[cursor] = ' '
      }
    }
    index = end - 1
  }
  return { code: output.join(''), hasSizeExemption, markerLines }
}

function maskCssProtected(text) {
  const output = [...text]
  for (let index = 0; index < text.length; index += 1) {
    let end = index
    if (text[index] === '"' || text[index] === "'") {
      end = consumeQuoted(text, index)
    } else if (isCssUrlStart(text, index)) {
      end = consumeCssUrl(text, index)
    }
    if (end === index) continue
    for (let cursor = index; cursor < end; cursor += 1) {
      if (output[cursor] !== '\n' && output[cursor] !== '\r') {
        output[cursor] = ' '
      }
    }
    index = end - 1
  }
  return output.join('')
}

function normalizePrelude(prelude) {
  return prelude
    .trim()
    .replace(/\s+/g, ' ')
    .replace(/\s*,\s*/g, ', ')
}

function firstContentOffset(text, start, end) {
  while (start < end && /\s/.test(text[start])) start += 1
  return start
}

function findDeclarationColon(segment) {
  let parentheses = 0
  let brackets = 0
  for (let index = 0; index < segment.length; index += 1) {
    if (segment[index] === '"' || segment[index] === "'") {
      index = consumeQuoted(segment, index) - 1
      continue
    }
    if (isCssUrlStart(segment, index)) {
      index = consumeCssUrl(segment, index) - 1
      continue
    }
    if (segment[index] === '(') parentheses += 1
    else if (segment[index] === ')') parentheses = Math.max(0, parentheses - 1)
    else if (segment[index] === '[') brackets += 1
    else if (segment[index] === ']') brackets = Math.max(0, brackets - 1)
    else if (segment[index] === ':' && parentheses === 0 && brackets === 0)
      return index
  }
  return -1
}

function cssColorLiterals(value) {
  const masked = maskCssProtected(value)
  const matches = []
  const hex =
    /#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{4}|[0-9a-f]{3})(?![A-Za-z0-9_-])/gi
  const functions =
    /(?<![A-Za-z0-9_-])(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\s*\(/gi
  for (const match of masked.matchAll(hex)) {
    matches.push({ index: match.index, text: match[0] })
  }
  for (const match of masked.matchAll(functions)) {
    matches.push({ index: match.index, text: match[0].trimEnd() })
  }
  for (const match of masked.matchAll(/[A-Za-z_-][A-Za-z0-9_-]*/g)) {
    if (
      NAMED_COLORS.has(match[0].toLowerCase()) &&
      masked[match.index - 1] !== '#'
    ) {
      matches.push({ index: match.index, text: match[0] })
    }
  }
  return matches.sort((left, right) => left.index - right.index)
}

function inspectDeclaration(state, start, end) {
  if (state.file === state.settings.palette || start >= end) return
  const segment = state.code.slice(start, end)
  const colon = findDeclarationColon(segment)
  if (colon === -1 || segment.trimStart().startsWith('@')) return
  const valueStart = start + colon + 1
  const value = state.code.slice(valueStart, end)
  for (const literal of cssColorLiterals(value)) {
    const line = state.lineAt(valueStart + literal.index)
    if (state.markerLines.has(line)) continue
    state.violations.push({
      file: state.file,
      line,
      rule: 'color-literal',
      message: `Color literal "${literal.text}" must be defined in ${state.settings.palette}.`,
    })
  }
}

function themeSelectorIndex(searchable, attributes) {
  const lower = searchable.toLowerCase()
  const indexes = attributes
    .map((attribute) => lower.indexOf(`[${attribute.toLowerCase()}`))
    .filter((index) => index !== -1)
  return indexes.length ? Math.min(...indexes) : -1
}

function inspectSelector(state, selector, start, context) {
  const normalized = normalizePrelude(selector)
  if (!normalized) return
  const line = state.lineAt(start)
  const key = `${context.join(' > ') || '<top>'}\0${normalized}`
  const first = state.selectors.get(key)
  if (first) {
    state.violations.push({
      file: state.file,
      line,
      rule: 'duplicate-selector',
      message: `Selector "${normalized}" duplicates ${first.file}:${first.line} in the same at-rule context.`,
    })
  } else {
    state.selectors.set(key, { file: state.file, line })
  }

  const { settings } = state
  const searchable = maskCssProtected(selector)
  const themeIndex = themeSelectorIndex(searchable, settings.themeAttributes)
  if (state.file !== settings.palette && themeIndex !== -1) {
    const themeLine = state.lineAt(start + themeIndex)
    if (!state.markerLines.has(themeLine)) {
      state.violations.push({
        file: state.file,
        line: themeLine,
        rule: 'theme-selector',
        message: `Theme selectors belong in ${settings.palette}.`,
      })
    }
  }

  for (const match of searchable.matchAll(/\.([A-Za-z_-][A-Za-z0-9_-]*)/g)) {
    const classLine = state.lineAt(start + match.index)
    const exempt = state.markerLines.has(classLine)
    state.classSelectors.push({
      className: match[1],
      exempt,
      file: state.file,
      line: classLine,
    })
    if (state.sourceTokens.has(match[1]) || exempt) continue
    state.violations.push({
      file: state.file,
      line: classLine,
      rule: 'unused-class',
      message: `Class ".${match[1]}" is not referenced by ${referenceSources(settings)}.`,
    })
  }
}

function inspectCssStatement(state, start, end) {
  const offset = firstContentOffset(state.code, start, end)
  const statement = state.code.slice(offset, end)
  const match =
    /^@import\s+(?:url\(\s*)?(?:"([^"]+)"|'([^']+)'|([^\s);]+))/i.exec(
      statement,
    )
  const specifier = match?.[1] ?? match?.[2] ?? match?.[3]
  if (!specifier) return
  state.cssImports.push({ line: state.lineAt(offset), specifier })
}

function atRuleFrame(prelude, parent) {
  const name = /^@([A-Za-z-]+)/.exec(prelude)?.[1].toLowerCase() ?? ''
  const context = [...parent.context]
  if (CONTEXT_AT_RULES.has(name)) {
    context.push(normalizePrelude(prelude))
  }
  let kind = 'rules'
  if (/(?:^|-)keyframes$/.test(name)) kind = 'keyframes'
  else if (DECLARATION_AT_RULES.has(name)) kind = 'declarations'
  return {
    context,
    insideStyle: parent.insideStyle || parent.kind === 'style',
    kind,
    segmentStart: 0,
  }
}

function openCssBlock(state, stack, end) {
  const parent = stack.at(-1)
  const start = firstContentOffset(state.code, parent.segmentStart, end)
  const prelude = state.code.slice(start, end).trimEnd()
  parent.segmentStart = end + 1
  if (prelude.startsWith('@')) {
    const frame = atRuleFrame(prelude, parent)
    frame.segmentStart = end + 1
    stack.push(frame)
    return
  }

  if (parent.kind === 'keyframes') {
    stack.push({
      context: parent.context,
      insideStyle: false,
      kind: 'keyframe-step',
      segmentStart: end + 1,
    })
    return
  }

  const nested = parent.kind === 'style' || parent.insideStyle
  if (nested) {
    state.violations.push({
      file: state.file,
      line: state.lineAt(start),
      rule: 'nesting',
      message: `Nested style rule "${normalizePrelude(prelude)}" is not allowed.`,
    })
  } else {
    inspectSelector(state, prelude, start, parent.context)
  }
  stack.push({
    context: parent.context,
    insideStyle: true,
    kind: 'style',
    segmentStart: end + 1,
  })
}

function parseCss(state) {
  const stack = [
    { context: [], insideStyle: false, kind: 'rules', segmentStart: 0 },
  ]
  let parentheses = 0
  let brackets = 0
  for (let index = 0; index < state.code.length; index += 1) {
    const character = state.code[index]
    if (character === '"' || character === "'") {
      index = consumeQuoted(state.code, index) - 1
      continue
    }
    if (isCssUrlStart(state.code, index)) {
      index = consumeCssUrl(state.code, index) - 1
      continue
    }
    if (character === '(') parentheses += 1
    else if (character === ')') parentheses = Math.max(0, parentheses - 1)
    else if (character === '[') brackets += 1
    else if (character === ']') brackets = Math.max(0, brackets - 1)
    else if (parentheses === 0 && brackets === 0 && character === '{') {
      openCssBlock(state, stack, index)
    } else if (parentheses === 0 && brackets === 0 && character === ';') {
      const frame = stack.at(-1)
      if (frame.kind === 'rules' && !frame.insideStyle) {
        inspectCssStatement(state, frame.segmentStart, index)
      } else if (
        frame.kind === 'style' ||
        frame.kind === 'declarations' ||
        frame.kind === 'keyframe-step' ||
        (frame.kind === 'rules' && frame.insideStyle)
      ) {
        inspectDeclaration(state, frame.segmentStart, index)
      }
      frame.segmentStart = index + 1
    } else if (parentheses === 0 && brackets === 0 && character === '}') {
      if (stack.length === 1) continue
      const frame = stack.pop()
      if (
        frame.kind === 'style' ||
        frame.kind === 'declarations' ||
        frame.kind === 'keyframe-step' ||
        (frame.kind === 'rules' && frame.insideStyle)
      ) {
        inspectDeclaration(state, frame.segmentStart, index)
      }
      stack.at(-1).segmentStart = index + 1
    }
  }
}

function scriptClassReferences(text, scan) {
  const references = []
  for (const span of scan.strings) {
    if (!span.reference) continue
    const value = text.slice(span.contentStart, span.contentEnd)
    for (const match of value.matchAll(/\S+/g)) {
      if (!/^[A-Za-z_-][A-Za-z0-9_-]*$/.test(match[0])) continue
      const line = scan.lineAt(span.contentStart + match.index)
      references.push({
        className: match[0],
        exempt: scan.markerLines.has(line),
        line,
      })
    }
  }
  return references
}

function referenceSources({ html, scriptExtensions, sourceRoots }) {
  const roots = sourceRoots.map((root) => `${root}/**`).join(', ')
  const extensions = scriptExtensions.map((ext) => ext.slice(1)).join(',')
  return [`${roots}/*.{${extensions}}`, ...html].join(' or ')
}

function inspectScriptColors(text, file, violations, scan, palette) {
  const { lineAt, markerLines, strings } = scan
  const hex =
    /([ '"`(:])(#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{4}|[0-9a-f]{3}))(?![A-Za-z0-9_-])/gi
  const functions =
    /(?<![A-Za-z0-9_-])(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\s*\(/gi
  for (const span of strings) {
    const value = text.slice(span.start, span.end)
    const matches = []
    for (const match of value.matchAll(hex)) {
      matches.push({ index: match.index + 1, text: match[2] })
    }
    for (const match of value.matchAll(functions)) {
      matches.push({ index: match.index, text: match[0].trimEnd() })
    }
    for (const literal of matches.sort(
      (left, right) => left.index - right.index,
    )) {
      const line = lineAt(span.start + literal.index)
      if (markerLines.has(line)) continue
      violations.push({
        file,
        line,
        rule: 'color-literal',
        message: `Color literal "${literal.text}" must be defined in ${palette}.`,
      })
    }
  }
}

function countLines(text) {
  if (!text) return 0
  const newlines = text.match(/\n/g)?.length ?? 0
  return newlines + (text.endsWith('\n') ? 0 : 1)
}

function existingHtml(root, html) {
  return html.map((file) => join(root, file)).filter((path) => existsSync(path))
}

// Words in scripts and HTML, plus classes a stylesheet uses through
// Tailwind's `@apply`.
function sourceTokens(scriptFiles, htmlFiles, cssFiles) {
  const paths = [...scriptFiles, ...htmlFiles]
  const tokens = new Set()
  for (const path of paths) {
    for (const match of readFileSync(path, 'utf8').matchAll(
      /[A-Za-z0-9_-]+/g,
    )) {
      tokens.add(match[0])
    }
  }
  for (const path of cssFiles) {
    for (const rule of readFileSync(path, 'utf8').matchAll(/@apply\s+([^;{}]+)/g)) {
      for (const word of rule[1].matchAll(/[A-Za-z0-9_-]+/g)) tokens.add(word[0])
    }
  }
  return tokens
}

function htmlClassReferences(htmlFiles) {
  return htmlFiles.flatMap((path) => fileClassReferences(path))
}

function fileClassReferences(path) {
  const text = readFileSync(path, 'utf8')
  const lineAt = lineLocator(text)
  const code = text.replace(/<!--[\s\S]*?-->/g, (comment) =>
    comment.replace(/[^\r\n]/g, ' '),
  )
  const references = []
  const attributes = /\bclass\s*=\s*(["'])([\s\S]*?)\1/gi
  for (const match of code.matchAll(attributes)) {
    const value = match[2]
    const quoteOffset = match[0].indexOf(match[1])
    const valueStart = match.index + quoteOffset + 1
    for (const token of value.matchAll(/\S+/g)) {
      if (!/^[A-Za-z_-][A-Za-z0-9_-]*$/.test(token[0])) continue
      references.push({
        className: token[0],
        line: lineAt(valueStart + token.index),
      })
    }
  }
  return references
}

function resolveCssImports(root, importer, specifiers, cssFiles) {
  const imports = new Set()
  for (const specifier of specifiers) {
    if (!/^\.\.?\//.test(specifier)) continue
    const cleanSpecifier = specifier.split(/[?#]/, 1)[0]
    const target = relativePath(
      root,
      resolve(dirname(join(root, importer)), cleanSpecifier),
    )
    if (cssFiles.has(target)) imports.add(target)
  }
  return [...imports].sort()
}

function inspectModule(path, root, context) {
  const text = readFileSync(path, 'utf8')
  const file = relativePath(root, path)
  const isJsx = context.settings.jsxExtensions.includes(extname(path))
  const scan = scanScript(text, { jsx: isJsx })
  inspectScriptColors(
    text,
    file,
    context.violations,
    scan,
    context.settings.palette,
  )
  return {
    file,
    imports: resolveCssImports(root, file, scan.cssImports, context.cssFiles),
    references: isJsx ? scriptClassReferences(text, scan) : [],
  }
}

function inspectStylesheet(path, root, context) {
  const text = readFileSync(path, 'utf8')
  const file = relativePath(root, path)
  const metadata = stripCssComments(text)
  const lines = countLines(text)
  const classSelectors = []
  const cssImports = []
  if (lines > context.limit && !metadata.hasSizeExemption) {
    context.violations.push({
      file,
      line: context.limit + 1,
      rule: 'size',
      message: `CSS file has ${lines} lines; limit is ${context.limit}.`,
    })
  }
  parseCss({
    classSelectors,
    code: metadata.code,
    cssImports,
    file,
    lineAt: lineLocator(text),
    markerLines: metadata.markerLines,
    selectors: context.selectors,
    settings: context.settings,
    sourceTokens: context.tokens,
    violations: context.violations,
  })
  return {
    classes: classSelectors,
    file,
    imports: resolveCssImports(
      root,
      file,
      cssImports.map(({ specifier }) => specifier),
      context.cssFiles,
    ),
  }
}

export function checkCss({ root, config }) {
  const absoluteRoot = resolve(root)
  const rawConfig = config ?? readCodeshapeConfig(absoluteRoot)
  const settings = configBlock(rawConfig, 'css', CSS_DEFAULTS)
  const { exclude, sourceRoots } = settings
  const cssFiles = sourceFiles(absoluteRoot, sourceRoots, ['.css'], exclude)
  const scriptFiles = sourceFiles(
    absoluteRoot,
    sourceRoots,
    settings.scriptExtensions,
    exclude,
  )
  const htmlFiles = existingHtml(absoluteRoot, settings.html)
  const context = {
    cssFiles: new Set(cssFiles.map((path) => relativePath(absoluteRoot, path))),
    limit: rawConfig.file_lines ?? DEFAULT_FILE_LINES,
    selectors: new Map(),
    settings,
    tokens: sourceTokens(scriptFiles, htmlFiles, cssFiles),
    violations: [],
  }
  const modules = scriptFiles.map((path) =>
    inspectModule(path, absoluteRoot, context),
  )
  const stylesheets = cssFiles.map((path) =>
    inspectStylesheet(path, absoluteRoot, context),
  )
  context.violations.push(
    ...checkCssOwnership({
      globalStyles: settings.globalStyles,
      indexReferences: htmlClassReferences(htmlFiles),
      modules,
      stylesheets,
    }),
  )
  return sortViolations(context.violations)
}

const modulePath = fileURLToPath(import.meta.url)
if (process.argv[1] && resolve(process.argv[1]) === modulePath) {
  runCheckCli({
    argv: process.argv.slice(2),
    check: checkCss,
    defaultRoot: resolve(dirname(modulePath), '..'),
    label: 'CSS check',
  })
}
