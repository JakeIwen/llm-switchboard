import { describe, expect, it } from 'vitest'
import { age, plural, size } from './format'

describe('age', () => {
  const now = 1_000_000
  it('names the coarsest whole unit', () => {
    expect(age(now - 30, now)).toBe('just now')
    expect(age(now - 5 * 60, now)).toBe('5m ago')
    expect(age(now - 3 * 3600 - 59, now)).toBe('3h ago')
    expect(age(now - 2 * 86400, now)).toBe('2d ago')
  })
  it('is empty for an unknown time', () => {
    expect(age(0, now)).toBe('')
    expect(age(null, now)).toBe('')
  })
})

describe('size and plural', () => {
  it('switches to MB at one mebibyte', () => {
    expect(size(812 * 1024)).toBe('812 KB')
    expect(size(1048576 * 19.1)).toBe('19.1 MB')
    expect(size(undefined)).toBe('')
  })
  it('pluralizes all but one', () => {
    expect(plural(1, 'session')).toBe('1 session')
    expect(plural(0, 'session')).toBe('0 sessions')
    expect(plural(6, 'match', 'matches')).toBe('6 matches')
  })
})
