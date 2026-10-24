import type { FolderChoice, FolderChoices } from './folderChoices'

const STORAGE_KEY = 'switchboard.dirs'
const LEGACY_KEY = 'switchboard.dir'
export type FolderStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export function readFolders(storage?: Pick<FolderStorage, 'getItem'>): Set<string> {
  try {
    const saved = storage!.getItem(STORAGE_KEY)
    if (saved !== null) {
      const keys: unknown = JSON.parse(saved)
      return new Set(Array.isArray(keys) ? keys.filter((key): key is string => typeof key === 'string' && Boolean(key)) : [])
    }
    const old = storage!.getItem(LEGACY_KEY)
    return new Set(old ? [old] : [])
  } catch { return new Set() }
}
export function writeFolders(storage: FolderStorage | undefined, keys: Iterable<string>): void {
  try {
    storage!.setItem(STORAGE_KEY, JSON.stringify([...new Set(keys)]))
    storage!.removeItem(LEGACY_KEY)
  } catch { /* Storage may be unavailable; in-memory selection still works. */ }
}

export function visibleChoices(choices: readonly FolderChoice[], text: string): FolderChoice[] {
  const query = text.trim().toLocaleLowerCase()
  return choices.filter((choice) => `${choice.project} ${choice.host || 'This Pi'}`.toLocaleLowerCase().includes(query))
}
export function groupChoices(choices: readonly FolderChoice[]): Map<string, FolderChoice[]> {
  const groups = new Map<string, FolderChoice[]>()
  for (const choice of choices) {
    if (!groups.has(choice.host)) groups.set(choice.host, [])
    groups.get(choice.host)!.push(choice)
  }
  return groups
}

/** DOM-free model of the existing persisted multiselect. */
export class FolderSelection {
  selected: Set<string>
  constructor(private readonly storage?: FolderStorage) { this.selected = readFolders(storage) }
  get single(): string | null { return this.selected.size === 1 ? [...this.selected][0] : null }
  matches(key: string): boolean { return !this.selected.size || this.selected.has(key) }
  set(keys: Iterable<string>): void {
    this.selected = new Set(keys)
    writeFolders(this.storage, this.selected)
  }
  toggleOnly(key: string): void { this.set(this.single === key ? [] : [key]) }
  invert(choices: readonly FolderChoice[], query = ''): void {
    const next = new Set(this.selected)
    for (const { key } of visibleChoices(choices, query)) {
      next.has(key) ? next.delete(key) : next.add(key)
    }
    this.set(next)
  }
  summary(model: FolderChoices): { picked: FolderChoice[]; count: number; total: number } {
    const picked = model.choices.filter((choice) => this.selected.has(choice.key))
    return { picked, count: picked.length ? picked.reduce((sum, choice) => sum + choice.count, 0) : model.total, total: model.total }
  }
}
