import { test } from 'vitest'
import assert from 'node:assert/strict'
import { session } from '../../actions/actionFixtures'
import { inFolders, matchingRows } from './pickerModel'

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

test('a picked machine takes every row on it, alongside picked folders', () => {
  const all = [
    session('pi-a', { project: '~/dev/a' }),
    session('pi-b', { project: '~/dev/b' }),
    session('mac:a', { host: 'mac', project: '~/dev/a' }),
  ]
  const picked = (...keys: string[]) => inFolders(all, new Set(keys)).map((r) => r.id).join(' ')
  assert.equal(picked('@mac'), 'mac:a')
  assert.equal(picked('@'), 'pi-a pi-b')
  assert.equal(picked('@mac', '~/dev/b'), 'pi-b mac:a')
})
