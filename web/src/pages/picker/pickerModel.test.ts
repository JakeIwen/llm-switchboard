import { test } from 'vitest'
import assert from 'node:assert/strict'
import { session } from '../../actions/actionFixtures'
import { matchingRows } from './pickerModel'

const rows = [
  session('typed'),
  session('probe', { headless: true }),
  session('kept-probe', { headless: true, enabled: true }),
]
const ids = (headless: boolean) =>
  matchingRows(rows, '', 'all', 'all', headless, 0).rows.map((r) => r.id).join(' ')

test('headless runs stay out unless asked for, except kept ones', () => {
  assert.equal(ids(false), 'typed kept-probe')
  assert.equal(ids(true), 'typed probe kept-probe')
})
