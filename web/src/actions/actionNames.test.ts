import { expect, it } from 'vitest'
import { forkName, nameTaken } from './actionNames'
import { mac, session } from './actionFixtures'

it('explains a cross-tool duplicate by whitespace/case and either cwd or display path, without counting the Mac', () => {
  const rows = [session('a', { tool: 'codex', title: ' A   name ' }), mac('b', { title: 'Remote name' })]
  expect(nameTaken(rows, ' /work/project/ ', 'a NAME')).toBe('~/project already has a session named “ A   name ”.')
  expect(nameTaken(rows, '~/project', 'A NAME')).toContain('already has a session')
  expect(nameTaken(rows, '/other', 'a name')).toBe('')
  expect(nameTaken(rows, '/work/project', 'Remote name')).toBe('')
})

it('suggests a free short fork title in the source folder, as the old fork dialog did', () => {
  const source = session('a', { title: 'x'.repeat(120), cwd: '/target' })
  const base = 'x'.repeat(100)
  const rows = [source, session('b', { title: `${base} (fork)`, cwd: '/target' }), session('c', { title: `${base} (fork 2)`, cwd: '/target' })]
  expect(forkName(rows, source)).toBe(`${base} (fork 3)`)
  expect(forkName(rows, { ...source, cwd: '/elsewhere' })).toBe(`${base} (fork)`)
})
