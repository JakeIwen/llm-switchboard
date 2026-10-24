// Lightweight scanner for TS/JS/JSX modules: finds string literals (and whether
// each is an object key), `codeshape: allow-marker` lines, and import
// specifiers, without a full parser. Shared by check-css.mjs and
// check-structure.mjs.
const JSX_PREFIX_TOKENS = new Set([
  '',
  '=',
  '(',
  '[',
  '{',
  ',',
  ':',
  '?',
  'return',
  '>',
  '&',
  '|',
  '!',
  ';',
])

export function lineLocator(text) {
  const starts = [0]
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === '\n') starts.push(index + 1)
  }
  return (offset) => {
    let low = 0
    let high = starts.length
    while (low + 1 < high) {
      const middle = Math.floor((low + high) / 2)
      if (starts[middle] <= offset) low = middle
      else high = middle
    }
    return low + 1
  }
}

export function consumeQuoted(text, start) {
  const quote = text[start]
  for (let index = start + 1; index < text.length; index += 1) {
    if (text[index] === '\\') index += 1
    else if (text[index] === quote) return index + 1
  }
  return text.length
}

export function addMarkerLines(comment, start, lineAt, markerLines) {
  for (const match of comment.matchAll(/codeshape:\s*allow-marker\b/g)) {
    markerLines.add(lineAt(start + match.index))
  }
}

class ScriptScanner {
  constructor(text, jsx) {
    this.text = text
    this.jsx = jsx
    this.lineAt = lineLocator(text)
    this.markerLines = new Set()
    this.strings = []
    this.cssImports = []
    this.imports = []
    this.comments = []
    this.stack = [{ type: 'code', templateExpression: false, depth: 0 }]
    this.tokens = ['', '', '']
    this.typeOnlyStatement = false
  }

  get lastCodeToken() {
    return this.tokens[2]
  }

  set lastCodeToken(token) {
    this.tokens = [this.tokens[1], this.tokens[2], token]
  }

  scan() {
    for (let index = 0; index < this.text.length; index += 1) {
      const type = this.stack.at(-1).type
      if (type === 'template') index = this.scanTemplate(index)
      else if (type === 'jsxText') index = this.scanJsxText(index)
      else if (type === 'jsxTag') index = this.scanJsxTag(index)
      else index = this.scanCode(index)
    }
    return {
      comments: this.comments,
      cssImports: this.cssImports,
      imports: this.imports,
      lineAt: this.lineAt,
      markerLines: this.markerLines,
      strings: this.strings,
    }
  }

  nextCodeIndex(index) {
    while (index < this.text.length) {
      if (/\s/.test(this.text[index])) index += 1
      else if (this.text[index] === '/' && this.text[index + 1] === '/') {
        const newline = this.text.indexOf('\n', index + 2)
        index = newline === -1 ? this.text.length : newline + 1
      } else if (this.text[index] === '/' && this.text[index + 1] === '*') {
        const close = this.text.indexOf('*/', index + 2)
        index = close === -1 ? this.text.length : close + 2
      } else {
        break
      }
    }
    return index
  }

  isObjectPropertyKey(end) {
    if (this.lastCodeToken !== '{' && this.lastCodeToken !== ',') return false
    return this.text[this.nextCodeIndex(end)] === ':'
  }

  addString(start, end, contentStart, contentEnd, reference = true) {
    this.strings.push({ start, end, contentStart, contentEnd, reference })
  }

  scanTemplate(index) {
    const frame = this.stack.at(-1)
    if (this.text[index] === '\\') return index + 1
    if (this.text[index] === '`') {
      this.addString(frame.segmentStart, index + 1, frame.contentStart, index)
      this.stack.pop()
      this.lastCodeToken = 'string'
    } else if (this.text[index] === '$' && this.text[index + 1] === '{') {
      this.addString(frame.segmentStart, index, frame.contentStart, index)
      this.stack.push({ type: 'code', templateExpression: true, depth: 1 })
      this.lastCodeToken = '{'
      return index + 1
    }
    return index
  }

  scanJsxText(index) {
    if (this.text[index] === '<') {
      this.stack.push({
        type: 'jsxTag',
        closing: this.text[index + 1] === '/',
      })
      this.lastCodeToken = '<'
    } else if (this.text[index] === '{') {
      this.stack.push({ type: 'code', jsxExpression: true, depth: 1 })
      this.lastCodeToken = '{'
    }
    return index
  }

  scanJsxTag(index) {
    const character = this.text[index]
    if (character === '"' || character === "'") {
      return this.recordQuoted(index)
    }
    if (character === '{') {
      this.stack.push({ type: 'code', jsxExpression: true, depth: 1 })
      this.lastCodeToken = '{'
    } else if (character === '>') {
      const frame = this.stack.pop()
      const selfClosing = /\/\s*$/.test(this.text.slice(0, index))
      if (frame.closing) {
        if (this.stack.at(-1)?.type === 'jsxText') this.stack.pop()
        this.lastCodeToken = 'jsx'
      } else if (!selfClosing) {
        this.stack.push({ type: 'jsxText' })
      }
    }
    return index
  }

  scanCode(index) {
    const character = this.text[index]
    const frame = this.stack.at(-1)
    if (character === '/' && this.text[index + 1] === '/') {
      return this.recordLineComment(index)
    }
    if (character === '/' && this.text[index + 1] === '*') {
      return this.recordBlockComment(index)
    }
    if (character === '"' || character === "'") return this.recordQuoted(index)
    if (character === '`') {
      this.stack.push({
        type: 'template',
        segmentStart: index,
        contentStart: index + 1,
      })
      this.lastCodeToken = 'string'
    } else if (
      (frame.templateExpression || frame.jsxExpression) &&
      character === '{'
    ) {
      frame.depth += 1
      this.lastCodeToken = '{'
    } else if (
      (frame.templateExpression || frame.jsxExpression) &&
      character === '}'
    ) {
      this.closeExpression(frame, index)
    } else if (this.startsJsx(index)) {
      this.stack.push({ type: 'jsxTag', closing: false })
      this.lastCodeToken = '<'
    } else if (/[A-Za-z_$]/.test(character)) {
      return this.recordIdentifier(index)
    } else if (!/\s/.test(character)) {
      this.lastCodeToken = character
    }
    return index
  }

  closeExpression(frame, index) {
    frame.depth -= 1
    this.lastCodeToken = '}'
    if (frame.depth !== 0) return
    this.stack.pop()
    const parent = this.stack.at(-1)
    if (parent.type === 'template') {
      parent.segmentStart = index + 1
      parent.contentStart = index + 1
    }
  }

  startsJsx(index) {
    if (!this.jsx || this.text[index] !== '<') return false
    if (!/[A-Za-z>]/.test(this.text[index + 1] ?? '')) return false
    return JSX_PREFIX_TOKENS.has(this.lastCodeToken)
  }

  recordLineComment(index) {
    const end = this.text.indexOf('\n', index + 2)
    const stop = end === -1 ? this.text.length : end
    this.recordComment(index, stop)
    return stop - 1
  }

  recordBlockComment(index) {
    const close = this.text.indexOf('*/', index + 2)
    const stop = close === -1 ? this.text.length : close + 2
    this.recordComment(index, stop)
    return stop - 1
  }

  recordComment(start, end) {
    this.comments.push({ start, end })
    addMarkerLines(
      this.text.slice(start, end),
      start,
      this.lineAt,
      this.markerLines,
    )
  }

  // Classifies a string literal by the code tokens before it: `import 'x'`,
  // `... from 'x'`, `import('x')`, `require('x')`.
  importKind() {
    const [beforePrevious, previous, last] = this.tokens
    if (last === 'import' && this.stack.length === 1) return 'side-effect'
    if (last === 'from' && this.stack.length === 1) return 'static'
    if (last === '(' && previous === 'import') return 'dynamic'
    if (last === '(' && previous === 'require' && beforePrevious !== '.') {
      return 'require'
    }
    return null
  }

  recordQuoted(index) {
    const end = consumeQuoted(this.text, index)
    const contentEnd = this.text[end - 1] === this.text[index] ? end - 1 : end
    this.addString(
      index,
      end,
      index + 1,
      contentEnd,
      !this.isObjectPropertyKey(end),
    )
    const value = this.text.slice(index + 1, contentEnd)
    const kind = this.importKind()
    if (kind) {
      this.imports.push({
        kind,
        line: this.lineAt(index),
        specifier: value,
        typeOnly: kind === 'static' && this.typeOnlyStatement,
      })
    }
    if ((kind === 'side-effect' || kind === 'static') && /^\.\.?\//.test(value)) {
      this.cssImports.push(value)
    }
    this.lastCodeToken = 'string'
    return end - 1
  }

  recordIdentifier(index) {
    let end = index + 1
    while (end < this.text.length && /[A-Za-z0-9_$]/.test(this.text[end])) {
      end += 1
    }
    const token = this.text.slice(index, end)
    if (token === 'import' || token === 'export') this.typeOnlyStatement = false
    if (token === 'type' && ['import', 'export'].includes(this.lastCodeToken)) {
      this.typeOnlyStatement = true
    }
    this.lastCodeToken = token
    return end - 1
  }
}

export function scanScript(text, { jsx = false } = {}) {
  return new ScriptScanner(text, jsx).scan()
}
