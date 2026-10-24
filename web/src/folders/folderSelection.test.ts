import { describe, expect, it } from 'vitest'
import { folderChoices } from './folderChoices'
import { FolderSelection, groupChoices, readFolders, visibleChoices, writeFolders, type FolderStorage } from './folderSelection'

function storage(values: Record<string, string> = {}): FolderStorage {
  return {
    getItem: (key) => values[key] ?? null,
    setItem: (key, value) => { values[key] = value },
    removeItem: (key) => { delete values[key] },
  }
}
function fixture() {
  const saved = storage()
  const sessions = ['alpha', 'alpha', 'beta', 'beta', 'gamma', 'gamma'].map((name) => ({ project: `~/dev/${name}` }))
  const filter = new FolderSelection(saved)
  const model = (matching = sessions) => folderChoices(sessions, filter.selected, {}, matching)
  return { saved, sessions, filter, model }
}

describe('folder preferences', () => {
  it('counts and fixed clear state follow other filters, not selected folders', () => {
    const { filter, model, sessions, saved } = fixture()
    filter.set(['~/dev/alpha'])
    const filtered = filter.summary(model([sessions[0], sessions[2]]))
    expect(filtered.count).toBe(1)
    expect(filtered.total).toBe(2)
    expect(filtered.picked.length).toBe(1)
    const empty = filter.summary(model([]))
    expect(empty.count).toBe(0)
    expect(empty.picked.length).toBe(1)
    expect([...readFolders(saved)]).toEqual(['~/dev/alpha'])
    filter.set([])
    expect(filter.summary(model([])).picked).toEqual([])
    expect([...readFolders(saved)]).toEqual([])
  })
  it('inverts visible choices and preserves selections outside the find box', () => {
    const { filter, model, saved } = fixture()
    filter.set(['~/dev/alpha'])
    filter.invert(model().choices)
    expect([...readFolders(saved)]).toEqual(['~/dev/beta', '~/dev/gamma'])
    filter.invert(model().choices, 'beta')
    expect([...readFolders(saved)]).toEqual(['~/dev/gamma'])
    filter.invert(model().choices, 'beta')
    expect([...readFolders(saved)]).toEqual(['~/dev/gamma', '~/dev/beta'])
    expect(visibleChoices(model().choices, 'nothing')).toEqual([])
    filter.invert(model().choices, 'nothing')
    expect([...readFolders(saved)]).toEqual(['~/dev/gamma', '~/dev/beta'])
  })
  it('deduplicates strings and rejects other entries', () => {
    expect([...readFolders(storage({ 'switchboard.dirs': '["~/dev/a","mac:~/dev/a","~/dev/a",null,2,""]' }))])
      .toEqual(['~/dev/a', 'mac:~/dev/a'])
  })
  it('migrates the legacy key only on save, including empty saves', () => {
    const values = { 'switchboard.dir': '~/dev/old' }
    const saved = storage(values)
    expect([...readFolders(saved)]).toEqual(['~/dev/old'])
    expect([...readFolders(storage({ 'switchboard.dirs': '[]', ...values }))]).toEqual([])
    new FolderSelection(saved).set(['mac:~/dev/a', '~/dev/a', '~/dev/a'])
    expect(values).toEqual({ 'switchboard.dirs': '["mac:~/dev/a","~/dev/a"]' })
    writeFolders(saved, [])
    expect(values).toEqual({ 'switchboard.dirs': '[]' })
  })
  it('keeps all folders available with invalid or unavailable storage', () => {
    for (const saved of ['broken JSON', '{}', 'null', '"~/dev/a"']) {
      expect(readFolders(storage({ 'switchboard.dirs': saved })).size).toBe(0)
    }
    expect(readFolders({ getItem() { throw new Error('blocked storage') } }).size).toBe(0)
    expect(readFolders(undefined).size).toBe(0)
  })
  it('keeps the in-memory selection if writes fail, without trying to migrate', () => {
    let removed = false
    const filter = new FolderSelection({ getItem: () => null, setItem() { throw Error('full') }, removeItem() { removed = true } })
    filter.set(['~/dev/a'])
    expect(filter.single).toBe('~/dev/a')
    expect(removed).toBe(false)
    const unavailable = new FolderSelection()
    unavailable.set(['~/dev/a'])
    expect(unavailable.matches('~/dev/a')).toBe(true)
    writeFolders({ ...storage(), removeItem() { throw Error('blocked') } }, ['~/dev/a'])
  })
  it('toggles an exclusive folder and matches all only when cleared', () => {
    const { filter } = fixture()
    expect(filter.single).toBeNull()
    expect(filter.matches('anything')).toBe(true)
    filter.toggleOnly('~/dev/alpha')
    expect(filter.single).toBe('~/dev/alpha')
    expect(filter.matches('~/dev/beta')).toBe(false)
    filter.toggleOnly('~/dev/alpha')
    expect(filter.selected.size).toBe(0)
    filter.set(['~/dev/alpha', '~/dev/beta'])
    expect(filter.single).toBeNull()
    filter.toggleOnly('~/dev/beta')
    expect([...filter.selected]).toEqual(['~/dev/beta'])
  })
  it('finds paths and machines case-insensitively, grouping in offered order', () => {
    const choices = folderChoices([], ['~/dev/Alpha', 'mac:~/dev/Alpha', 'mac:~/dev/Beta']).choices
    expect(visibleChoices(choices, ' ALPHA ').map((c) => c.key)).toEqual(['~/dev/Alpha', 'mac:~/dev/Alpha'])
    expect(visibleChoices(choices, 'this pi').map((c) => c.key)).toEqual(['~/dev/Alpha'])
    expect(visibleChoices(choices, 'MAC').map((c) => c.key)).toEqual(['mac:~/dev/Alpha', 'mac:~/dev/Beta'])
    const groups = groupChoices(choices)
    expect([...groups.keys()]).toEqual(['', 'mac'])
    expect(groups.get('mac')?.map((c) => c.project)).toEqual(['~/dev/Alpha', '~/dev/Beta'])
    expect([...groupChoices([])]).toEqual([])
  })
})
