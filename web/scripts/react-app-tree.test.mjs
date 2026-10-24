// The app's own tree must pass both react-app checks. Installed with the
// checkers; the fixture tests beside it cover the rules themselves.
import assert from 'node:assert/strict'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { checkCss } from './check-css.mjs'
import { checkStructure } from './check-structure.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')

function summary(violations) {
  return violations
    .map(({ file, line, rule }) => `${file}:${line} ${rule}`)
    .join('\n')
}

test('the app has no CSS code-shape violations', () => {
  const violations = checkCss({ root })
  assert.equal(violations.length, 0, summary(violations))
})

test('the app has no structural violations', () => {
  const violations = checkStructure({ root })
  assert.equal(violations.length, 0, summary(violations))
})
