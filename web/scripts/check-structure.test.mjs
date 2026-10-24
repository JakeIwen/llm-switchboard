import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { checkStructure } from './check-structure.mjs'

function fixture(t, files, config) {
  const root = mkdtempSync(join(tmpdir(), 'react-app-structure-check-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  if (config) files['.codeshape/config.json'] = JSON.stringify(config)
  for (const [name, contents] of Object.entries(files)) {
    const path = join(root, name)
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, contents)
  }
  return root
}

function results(root, rule) {
  return checkStructure({ root })
    .filter((violation) => !rule || violation.rule === rule)
    .map(({ file, line, rule: name }) => `${file}:${line} ${name}`)
}

test('contracts: content, components and dev imports', (t) => {
  const root = fixture(t, {
    'src/main.tsx': `import { App } from './App'
if (import.meta.env.DEV) void import('./dev/mountPanel')
import './dev/side-effect'`,
    'src/App.tsx': `import { Panel } from './dev/Panel'
export function App() { return <Panel /> }`,
    'src/dev/Panel.tsx': 'export function Panel() { return <aside /> }',
    'src/dev/mountPanel.tsx': "import { Panel } from './Panel'",
    'src/dev/side-effect.ts': 'export {}',
    'src/content/site.ts': `import { Card } from '../components/Card'
import type { CardProps } from '../components/Card'
export const site = { Card }`,
    'src/components/Card.tsx': `import { homePath } from '../pages/paths'
export type CardProps = {}
export function Card() { return <a href={homePath} /> }`,
    'src/pages/paths.ts': "export const homePath = '/'",
  })
  assert.deepEqual(results(root, 'contract'), [
    'src/App.tsx:1 contract',
    'src/components/Card.tsx:1 contract',
    'src/content/site.ts:1 contract',
    'src/content/site.ts:2 contract',
    'src/main.tsx:3 contract',
  ])
})

test('cycles: value imports only, reported once per cycle', (t) => {
  const root = fixture(t, {
    'src/a.ts': "import { b } from './b'\nexport const a = () => b",
    'src/b.ts': "import { c } from './c'\nexport const b = () => c",
    'src/c.ts': "import { a } from './a.js'\nexport const c = () => a",
    'src/types.ts': "import type { T2 } from './types2'\nexport type T1 = T2",
    'src/types2.ts': "import type { T1 } from './types'\nexport type T2 = T1",
    'src/lazy.ts': "export const load = () => import('./lazy2')",
    'src/lazy2.ts': "import { load } from './lazy'\nexport { load }",
  })
  const violations = checkStructure({ root }).filter((item) => item.rule === 'cycle')
  assert.equal(violations.length, 1)
  assert.equal(violations[0].file, 'src/a.ts')
  assert.equal(
    violations[0].message,
    'Import cycle among src/a.ts, src/b.ts, src/c.ts.',
  )
})

test('colocate-component: single-consumer components sit in its folder', (t) => {
  const root = fixture(t, {
    'src/App.tsx': `import { Header } from './components/layout/Header'
import { HomePage } from './pages/HomePage'
export function App() { return <><Header /><HomePage /></> }`,
    'src/components/layout/Header.tsx': 'export function Header() { return <header /> }',
    'src/pages/HomePage.tsx': `import { Hero } from './home/Hero'
import { Preview } from '../components/projects/Preview'
import { Shared } from '../components/Shared'
import { useThing } from '../components/useThing'
export function HomePage() { useThing(); return <><Hero /><Preview /><Shared /></> }`,
    'src/pages/home/Hero.tsx': `import { Shared } from '../../components/Shared'
export function Hero() { return <Shared /> }`,
    'src/components/projects/Preview.tsx': 'export const Preview = () => <img />',
    'src/components/Shared.tsx': 'export const Shared = () => <span />',
    'src/components/useThing.ts': 'export function useThing() {}',
    'src/components/Lonely.test.tsx': "import { Shared } from './Shared'",
  })
  assert.deepEqual(results(root, 'colocate-component'), [
    'src/components/projects/Preview.tsx:1 colocate-component',
  ])
})

test('one-component: counts component exports in every form', (t) => {
  const root = fixture(t, {
    'src/Fine.tsx': `import { memo } from 'react'
export type FineProps = { label: string }
export const fineLabel = 'Fine'
export const Sizes = ['s', 'm']
export function useFine() { return 1 }
function Fine({ label }: FineProps) { return <b>{label}</b> }
export default memo(Fine)
export { Fine }`,
    'src/Two.tsx': `export function First() { return <i /> }
export const Second = ({ a }: { a: string }): JSX.Element => <b>{a}</b>`,
    'src/Wrapped.tsx': `import { forwardRef, Component } from 'react'
export const Input = forwardRef<HTMLInputElement>((props, ref) => <input ref={ref} />)
class Legacy extends Component { render() { return null } }
const Note = () => <p />
export { Legacy, Note as Remark }`,
    'src/Marked.jsx': `export function A() { return <i /> }
export function B() { return <b /> } // codeshape: allow-marker`,
    'src/Anonymous.jsx': `export const Label = () => <span />
export default function () { return <div class="x" /> }`,
  })
  const violations = checkStructure({ root }).filter((item) => item.rule === 'one-component')
  assert.deepEqual(
    violations.map(({ file, line, message }) => `${file}:${line} ${message}`),
    [
      'src/Anonymous.jsx:2 Exports 2 components (Label, default); give default its own file.',
      'src/Two.tsx:2 Exports 2 components (First, Second); give Second its own file.',
      'src/Wrapped.tsx:5 Exports 3 components (Input, Legacy, Note); give Legacy its own file.',
    ],
  )
})

test('structure block: source root, aliases, settings typos', (t) => {
  const root = fixture(
    t,
    {
      'app/root.tsx': `import { Nav } from '~/components/Nav'
export default function App() { return <Nav /> }`,
      'app/components/Nav.tsx': `import { routes } from '~/routes/list'
export function Nav() { return <nav /> }`,
      'app/routes/list.ts': "import { Nav } from '~/components/Nav'\nexport const routes = [Nav]",
    },
    { structure: { sourceRoot: 'app', aliases: { '~/': 'app/' } } },
  )
  assert.deepEqual(results(root), ['app/components/Nav.tsx:1 cycle'])
  assert.throws(
    () => checkStructure({ root, config: { structure: { sourceroot: 'app' } } }),
    /Unknown structure setting\(s\).*sourceroot/,
  )
})

test('ignoredExports leaves framework route-module exports alone', (t) => {
  const root = fixture(
    t,
    {
      'app/root.tsx': `export function Layout({ children }) { return <html>{children}</html> }
export default function App() { return <main /> }
export function ErrorBoundary() { return <p /> }`,
    },
    {
      structure: {
        sourceRoot: 'app',
        ignoredExports: ['Layout', 'ErrorBoundary', 'HydrateFallback'],
      },
    },
  )
  assert.deepEqual(results(root), [])
})

test('components behind import() are code-split boundaries, not colocation targets', (t) => {
  const root = fixture(t, {
    'src/main.jsx': "import { App } from './app/App'",
    'src/app/App.jsx': `const views = { drive: () => import('../views/Drive') }
export function App() { return <main /> }`,
    'src/views/Drive.jsx': `import { Gauge } from '../components/Gauge'
export default function Drive() { return <Gauge /> }`,
    'src/components/Gauge.jsx': 'export function Gauge() { return <i /> }',
  })
  assert.deepEqual(results(root, 'colocate-component'), [
    'src/components/Gauge.jsx:1 colocate-component',
  ])
})
