// Finds the components a .tsx/.jsx module exports, by pattern rather than a
// full parser: PascalCase functions, arrow functions, memo/forwardRef/lazy
// wrappers and class components, whether exported inline, by `export { }`
// or by `export default`. Types, hooks and camelCase values are ignored.
import { lineLocator } from './script-scanner.mjs'

const WRAPPER = /^(?:React\.)?(?:memo|forwardRef|lazy)\s*[<(]/
const CLASS_COMPONENT = /^[^{]*?\bextends\s+[\w$.]*Component\b/

// Blanks comments and string contents so patterns only see code.
function maskedCode(text, scan) {
  const output = [...text]
  const blank = (start, end) => {
    for (let index = start; index < end; index += 1) {
      if (output[index] !== '\n') output[index] = ' '
    }
  }
  for (const { start, end } of scan.comments) blank(start, end)
  for (const { contentStart, contentEnd } of scan.strings) {
    blank(contentStart, contentEnd)
  }
  return output.join('')
}

function matchingClose(code, start, open, close) {
  let depth = 0
  for (let index = start; index < code.length; index += 1) {
    if (code[index] === open) depth += 1
    else if (code[index] === close && (depth -= 1) === 0) return index + 1
  }
  return code.length
}

// True when the expression starting at `index` is a function or a React
// wrapper around one.
function isComponentInitializer(code, index) {
  let rest = code.slice(index).trimStart().replace(/^async\b\s*/, '')
  if (/^function\b/.test(rest) || WRAPPER.test(rest)) return true
  if (rest.startsWith('<')) {
    rest = rest.slice(matchingClose(rest, 0, '<', '>')).trimStart()
  }
  if (/^[A-Za-z_$][\w$]*\s*=>/.test(rest)) return true
  if (!rest.startsWith('(')) return false
  const afterParameters = rest.slice(matchingClose(rest, 0, '(', ')'))
  return /^\s*(?::[^=;{]*)?=>/.test(afterParameters)
}

function localComponents(code) {
  const names = new Set()
  for (const match of code.matchAll(/(?<![\w$.])function\s*\*?\s+([A-Z][\w$]*)/g)) {
    names.add(match[1])
  }
  const declarations = /(?<![\w$.])(?:const|let|var)\s+([A-Z][\w$]*)\s*(?::[^=]*)?=(?!=)/g
  for (const match of code.matchAll(declarations)) {
    if (isComponentInitializer(code, match.index + match[0].length)) {
      names.add(match[1])
    }
  }
  for (const match of code.matchAll(/(?<![\w$.])class\s+([A-Z][\w$]*)/g)) {
    if (CLASS_COMPONENT.test(code.slice(match.index))) names.add(match[1])
  }
  return names
}

function exportedNames(code, locals) {
  const found = []
  const add = (local, index) => found.push({ local, index })
  const inline =
    /\bexport\s+(?:default\s+)?(?:async\s+)?(?:function\s*\*?\s*|class\s+|(?:const|let|var)\s+)([A-Z][\w$]*)/g
  for (const match of code.matchAll(inline)) {
    if (locals.has(match[1])) add(match[1], match.index)
  }
  for (const match of code.matchAll(/\bexport\s+default\s+/g)) {
    const rest = code.slice(match.index + match[0].length)
    const wrapped = /^(?:React\.)?(?:memo|forwardRef)\s*\(\s*([A-Z][\w$]*)\s*\)/.exec(rest)
    const named = /^([A-Z][\w$]*)\s*(?:;|$)/m.exec(rest)
    if (wrapped) add(wrapped[1], match.index)
    else if (named && locals.has(named[1])) add(named[1], match.index)
    else if (/^(?:async\s+)?(?:function|class)\b\s*\*?\s*[A-Z]/.test(rest)) continue
    else if (
      /^(?:async\s+)?function\s*\*?\s*\(/.test(rest) ||
      /^class\s+extends\s+[\w$.]*Component\b/.test(rest) ||
      isComponentInitializer(code, match.index + match[0].length)
    ) {
      add('default', match.index)
    }
  }
  for (const match of code.matchAll(/\bexport\s*\{([^}]*)\}(?!\s*from)/g)) {
    for (const part of match[1].split(',')) {
      const local = part.trim().replace(/^type\s+/, '').split(/\s+as\s+/)[0]
      if (locals.has(local)) add(local, match.index)
    }
  }
  return found
}

export function componentExports(text, scan) {
  const code = maskedCode(text, scan)
  const lineAt = lineLocator(text)
  const seen = new Set()
  return exportedNames(code, localComponents(code))
    .sort((left, right) => left.index - right.index)
    .filter(({ local }) => !seen.has(local) && seen.add(local))
    .map(({ local, index }) => ({ name: local, line: lineAt(index) }))
}
