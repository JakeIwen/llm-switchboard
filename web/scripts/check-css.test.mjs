import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { checkCss } from './check-css.mjs'

function fixture(t, { files = {}, source = '', config } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'react-app-css-check-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  mkdirSync(join(root, 'src'), { recursive: true })
  writeFileSync(join(root, 'src/App.tsx'), source)
  writeFileSync(join(root, 'index.html'), '<main class="html-class"></main>')
  if (config) {
    mkdirSync(join(root, '.codeshape'), { recursive: true })
    writeFileSync(join(root, '.codeshape/config.json'), JSON.stringify(config))
  }
  for (const [name, contents] of Object.entries(files)) {
    const path = join(root, name)
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, contents)
  }
  return root
}

// Assembled so the repo's own code-shape scan doesn't read this test file as
// carrying a size exemption.
const sizeExemption = ['codeshape:', 'allow-size'].join(' ')

function violationsFor(root, rule) {
  return checkCss({ root }).filter((violation) => violation.rule === rule)
}

test('size accepts the limit, rejects overflow, and requires an exemption reason', (t) => {
  const root = fixture(t, {
    config: { file_lines: 2 },
    files: { 'src/app.css': '.used {\n  display: block;\n}' },
    source: 'export const classes = "used"',
  })
  assert.deepEqual(violationsFor(root, 'size'), [
    {
      file: 'src/app.css',
      line: 3,
      rule: 'size',
      message: 'CSS file has 3 lines; limit is 2.',
    },
  ])

  writeFileSync(join(root, 'src/app.css'), '.used {\ndisplay: block;')
  assert.deepEqual(violationsFor(root, 'size'), [])

  writeFileSync(
    join(root, 'src/app.css'),
    `/* ${sizeExemption} generated selectors */\n.used {\n  display: block;\n}`,
  )
  assert.deepEqual(violationsFor(root, 'size'), [])

  writeFileSync(
    join(root, 'src/app.css'),
    `/* ${sizeExemption} */\n.used {\n  display: block;\n}`,
  )
  assert.equal(violationsFor(root, 'size').length, 1)
})

test('color-literal accepts tokens and palette literals but rejects CSS colors', (t) => {
  const root = fixture(t, {
    files: {
      'src/app.css': `
.hex { color: #abc; }
.function { color: rgb(1 2 3); }
.modern { color: oklch(50% 0.1 20); }
.named { color: rebeccapurple; }
.mixed { color: color-mix(in srgb, red, var(--ink)); }
.allowed {
  color: var(--ink);
  border-color: transparent currentColor inherit initial unset;
  background: color-mix(in srgb, var(--ink), var(--paper));
  background-image: url("icon-#fff-red.svg");
  content: "red #fff rgb(1 2 3)";
}
.marker { color: #def; } /* codeshape: allow-marker */
`,
      'src/styles/palette.css': `
:root { --ink: #123456; }
[data-theme="dark"] { --ink: rgb(1 2 3); color: black; }
`,
    },
    source:
      'export const classes = "hex function modern named mixed allowed marker"',
  })
  const violations = violationsFor(root, 'color-literal')
  assert.deepEqual(
    violations.map((violation) => violation.message),
    [
      'Color literal "#abc" must be defined in src/styles/palette.css.',
      'Color literal "rgb(" must be defined in src/styles/palette.css.',
      'Color literal "oklch(" must be defined in src/styles/palette.css.',
      'Color literal "rebeccapurple" must be defined in src/styles/palette.css.',
      'Color literal "red" must be defined in src/styles/palette.css.',
    ],
  )
})

test('color-literal checks TS and TSX strings without treating anchors as colors', (t) => {
  const root = fixture(t, {
    files: {
      'src/View.tsx': `
export const View = () => <svg fill="#fff" />
const allowed = "#recommendation #main #abcde"
const embedded = "color: #aabbcc"
const fn = 'hsl(0 0% 0%)'
const nested = \`\${true ? '#1234' : 'var(--ink)'}\`
const ignored = '#def' // codeshape: allow-marker
// const comment = '#000'
`,
    },
    source: `
const direct = '#abc'
const fn = "rgb(1 2 3)"
const ignored = '#def' // codeshape: allow-marker
`,
  })
  assert.deepEqual(
    violationsFor(root, 'color-literal').map((violation) => violation.message),
    [
      'Color literal "#abc" must be defined in src/styles/palette.css.',
      'Color literal "rgb(" must be defined in src/styles/palette.css.',
      'Color literal "#fff" must be defined in src/styles/palette.css.',
      'Color literal "#aabbcc" must be defined in src/styles/palette.css.',
      'Color literal "hsl(" must be defined in src/styles/palette.css.',
      'Color literal "#1234" must be defined in src/styles/palette.css.',
    ],
  )
})

test('theme-selector is limited to the palette and honors line markers', (t) => {
  const root = fixture(t, {
    files: {
      'src/app.css': `
[data-theme="dark"] { color: var(--ink); }
html[data-theme="soft"] { color: var(--ink); } /* codeshape: allow-marker */
`,
      'src/styles/palette.css':
        '[data-theme="dark"] { --ink: var(--dark-ink); }',
    },
  })
  assert.deepEqual(violationsFor(root, 'theme-selector'), [
    {
      file: 'src/app.css',
      line: 2,
      rule: 'theme-selector',
      message: 'Theme selectors belong in src/styles/palette.css.',
    },
  ])
})

test('unused-class uses whole source tokens and honors selector markers', (t) => {
  const root = fixture(t, {
    files: {
      'src/app.css': `
.used, .dynamic-slug, .html-class, .card-wide { display: block; }
.card { display: block; }
.unused { display: block; }
.ignored { display: block; } /* codeshape: allow-marker */
`,
    },
    source: 'export const classes = "used dynamic-slug card-wide"',
  })
  assert.deepEqual(
    violationsFor(root, 'unused-class').map((violation) => violation.message),
    [
      'Class ".card" is not referenced by src/**/*.{ts,tsx,js,jsx,mjs} or index.html.',
      'Class ".unused" is not referenced by src/**/*.{ts,tsx,js,jsx,mjs} or index.html.',
    ],
  )
})

test('duplicate-selector compares normalized lists in media and supports contexts', (t) => {
  const root = fixture(t, {
    files: {
      'src/a.css': `
.same, .other { display: block; }
@media (min-width: 10px) { .responsive { display: block; } }
@supports (display: grid) { .supported { display: grid; } }
@keyframes first { from { opacity: 0; } 50% { opacity: 1; } to { opacity: 0; } }
`,
      'src/b.css': `
.same,.other { display: flex; }
@media   (min-width: 10px) { .responsive { display: flex; } }
@media (max-width: 9px) { .responsive { display: none; } }
@supports   (display: grid) { .supported { display: block; } }
@keyframes second { from { opacity: 0; } 50% { opacity: 1; } to { opacity: 0; } }
`,
    },
    source: 'export const classes = "same other responsive supported"',
  })
  const violations = violationsFor(root, 'duplicate-selector')
  assert.equal(violations.length, 3)
  assert.ok(
    violations.every((violation) =>
      violation.message.includes('duplicates src/a.css:'),
    ),
  )
})

test('nesting rejects style rules inside style rules without mis-parsing them', (t) => {
  const root = fixture(t, {
    files: {
      'src/app.css': `
.flat { color: var(--ink); }
.parent {
  color: var(--ink);
  .child { color: var(--ink); }
}
`,
    },
    source: 'export const classes = "flat parent child"',
  })
  assert.deepEqual(violationsFor(root, 'nesting'), [
    {
      file: 'src/app.css',
      line: 5,
      rule: 'nesting',
      message: 'Nested style rule ".child" is not allowed.',
    },
  ])
})

test('tokenizer ignores comments, strings, and URL contents', (t) => {
  const root = fixture(t, {
    files: {
      'src/app.css': `
/* .dead { color: #fff; } [data-theme="dark"] */
.safe[data-label="}; .fake, red"] {
  content: "red; } .ghost { color: #fff";
  background-image: url("data:image/svg+xml,<svg fill='#fff'>{;}</svg>");
}
`,
    },
    source: 'import "./app.css"\nexport const classes = "safe"',
  })
  assert.deepEqual(checkCss({ root }), [])
})

test('colocate moves single-module global classes but keeps shared and HTML classes global', (t) => {
  const root = fixture(t, {
    files: {
      'index.html': '<main class="html-only"></main>',
      'src/Other.tsx': 'export const other = "shared"',
      'src/styles/base.css': `
.solo { display: block; }
.nested { display: block; }
.shared { display: block; }
.html-only { display: block; }
.ignored { display: block; } /* codeshape: allow-marker */
`,
    },
    source: `
import './styles/base.css'
export const classes = 'solo shared ignored'
export const nested = \`base \${active ? 'nested' : ''}\`
`,
  })
  assert.deepEqual(violationsFor(root, 'colocate'), [
    {
      file: 'src/styles/base.css',
      line: 2,
      rule: 'colocate',
      message:
        'Class ".solo" is only used by src/App.tsx; move it to a stylesheet beside it.',
    },
    {
      file: 'src/styles/base.css',
      line: 3,
      rule: 'colocate',
      message:
        'Class ".nested" is only used by src/App.tsx; move it to a stylesheet beside it.',
    },
  ])
})

test('ownership follows direct imports and precise TSX string references', (t) => {
  const root = fixture(t, {
    files: {
      'src/Card.css': `
.card { display: block; }
.conditional { display: block; }
.jsx-text { display: block; }
.content-only { display: block; }
`,
      'src/Card.tsx': `
import './Card.css'
export const Card = ({ variant }) => (
  <div className={\`card \${variant ? 'conditional' : ''}\`}>
    jsx-text
  </div>
)
`,
      'src/Other.tsx': 'export const otherClass = "card"',
      'src/TextOnly.tsx': 'export const TextOnly = () => <p>card</p>',
      'src/projectMedia.tsx':
        "export const projectMedia = { 'card': <span /> }",
      'src/One.css': '.one.is-active { display: block; }',
      'src/One.tsx': `import './One.css'\nexport const one = 'one is-active'`,
      'src/Two.css': '.two.is-active { display: block; }',
      'src/Two.tsx': `import './Two.css'\nexport const two = 'two is-active'`,
      'src/content/data.ts':
        'export const classes = "card content-only is-active"',
    },
  })
  assert.deepEqual(checkCss({ root }), [
    {
      file: 'src/Other.tsx',
      line: 1,
      rule: 'ownership',
      message:
        'Class ".card" is defined in src/Card.css, but that stylesheet is not imported by src/Other.tsx.',
    },
  ])
})

test('coupling accepts parent-passed classes and rejects child-owned selector classes', (t) => {
  const root = fixture(t, {
    files: {
      'src/Hero.css': `
.hero-van { display: block; }
.hero-van .van-body { display: block; }
`,
      'src/Hero.tsx': `
import './Hero.css'
export const Hero = () => <Illustration className="hero-van" />
`,
      'src/Illustrations.tsx':
        'export const Illustration = () => <svg className="van-body" />',
    },
  })
  assert.deepEqual(violationsFor(root, 'coupling'), [
    {
      file: 'src/Hero.css',
      line: 3,
      rule: 'coupling',
      message:
        'Class ".van-body" is not referenced by any module that imports src/Hero.css.',
    },
  ])
})

test('coupling permits component context around a global class', (t) => {
  const root = fixture(t, {
    files: {
      'src/Button.tsx': 'export const Button = () => <a className="button" />',
      'src/Contact.css': '.contact .button { display: inline-flex; }',
      'src/Contact.tsx': `
import './Contact.css'
export const Contact = () => <section className="contact" />
`,
      'src/styles/base.css': '.button { display: inline-flex; }',
    },
    source: "import './styles/base.css'",
  })
  assert.deepEqual(violationsFor(root, 'coupling'), [])
})

test('orphan-stylesheet follows reachable CSS import chains', (t) => {
  const root = fixture(t, {
    files: {
      'src/orphan.css': 'aside { display: block; }',
      'src/styles/base.css': `@import url('./nested.css');\nhtml { display: block; }`,
      'src/styles/deep.css': 'main { display: block; }',
      'src/styles/nested.css': `@import './deep.css';\nbody { display: block; }`,
    },
    source: "import './styles/base.css'",
  })
  assert.deepEqual(violationsFor(root, 'orphan-stylesheet'), [
    {
      file: 'src/orphan.css',
      line: 1,
      rule: 'orphan-stylesheet',
      message:
        'Stylesheet "src/orphan.css" is not reachable from a relative import in a script module.',
    },
  ])
})

test('ownership and coupling require the rendering module to import the sheet', (t) => {
  const root = fixture(t, {
    files: {
      'src/dev/PalettePanel.tsx':
        'export const PalettePanel = () => <aside className="palette-panel" />',
      'src/dev/mountPalettePanel.tsx': "import './palette-panel.css'",
      'src/dev/palette-panel.css': '.palette-panel { display: block; }',
    },
  })
  assert.equal(violationsFor(root, 'ownership').length, 1)
  assert.equal(violationsFor(root, 'coupling').length, 1)
  assert.deepEqual(violationsFor(root, 'orphan-stylesheet'), [])
})

test('allow-marker exempts new line-level organization rules', (t) => {
  const root = fixture(t, {
    files: {
      'src/Owner.css':
        '.local { display: block; } /* codeshape: allow-marker */',
      'src/Owner.tsx': "import './Owner.css'",
      'src/Other.tsx':
        'export const local = "local" // codeshape: allow-marker',
      'src/styles/base.css':
        '.global { display: block; } /* codeshape: allow-marker */',
    },
    source: `import './styles/base.css'\nexport const global = 'global'`,
  })
  assert.deepEqual(violationsFor(root, 'colocate'), [])
  assert.deepEqual(violationsFor(root, 'ownership'), [])
  assert.deepEqual(violationsFor(root, 'coupling'), [])
})

test('data-palette selectors also belong in the palette', (t) => {
  const root = fixture(t, {
    files: {
      'src/app.css': ":root[data-palette='sea'] { --hue-brand: 220; }",
      'src/styles/palette.css': ":root[data-palette='sun'] { --hue-brand: 60; }",
    },
  })
  assert.deepEqual(
    violationsFor(root, 'theme-selector').map(({ file, line }) => `${file}:${line}`),
    ['src/app.css:1'],
  )
})

test('the css block relocates sources, palette, global layer and HTML', (t) => {
  const root = fixture(t, {
    config: {
      css: {
        sourceRoots: ['app/client'],
        palette: 'app/client/theme/tokens.css',
        globalStyles: 'app/client/theme/',
        html: ['app/client/index.html'],
      },
    },
    files: {
      'app/client/index.html': '<body class="page"></body>',
      'app/client/theme/tokens.css': ':root { --ink: #123; }',
      'app/client/theme/base.css': '.page { color: var(--ink); }',
      'app/client/main.tsx': "import './theme/tokens.css'\nimport './theme/base.css'\nimport './Panel.css'\nexport const Panel = () => <div className=\"panel\" />",
      'app/client/Panel.css': '.panel { color: #fff; }',
      'src/ignored.css': '.ignored { color: red; }',
    },
  })
  assert.deepEqual(
    checkCss({ root }).map(({ file, rule }) => `${file} ${rule}`),
    ['app/client/Panel.css color-literal'],
  )
  assert.match(
    violationsFor(root, 'color-literal')[0].message,
    /must be defined in app\/client\/theme\/tokens\.css/,
  )
})

test('an explicit config object overrides the repo file, and typos fail', (t) => {
  const root = fixture(t, {
    config: { css: { palette: 'src/nowhere.css' } },
    files: { 'src/styles/palette.css': ':root { --ink: #123; }' },
    source: "import './styles/palette.css'",
  })
  assert.equal(violationsFor(root, 'color-literal').length, 1)
  assert.deepEqual(checkCss({ root, config: {} }), [])
  assert.throws(
    () => checkCss({ root, config: { css: { pallete: 'x' } } }),
    /Unknown css setting\(s\).*pallete/,
  )
})

test('Preact class attributes in .jsx count as references', (t) => {
  const root = fixture(t, {
    files: {
      'src/Gauge.css': '.gauge { display: block; }\n.needle { display: block; }',
      'src/Gauge.jsx': "import './Gauge.css'\nexport const Gauge = () => <div class=\"gauge\"><i class={`needle`} /></div>",
      'src/Other.jsx': 'export const Other = () => <div class="gauge" />',
    },
  })
  assert.deepEqual(
    checkCss({ root }).map(({ file, rule }) => `${file} ${rule}`),
    ['src/Other.jsx ownership'],
  )
})

test('CSS Modules imported with from are reachable and not ownership-checked', (t) => {
  const root = fixture(t, {
    files: {
      'src/Card.module.css': '.title { display: block; }',
      'src/Card.tsx':
        "import styles from './Card.module.css'\nexport const Card = () => <h2 className={styles.title} />",
    },
  })
  assert.deepEqual(checkCss({ root }), [])
})

test('test files are not sources', (t) => {
  const root = fixture(t, {
    files: {
      'src/app.css': '.only-in-test { display: block; }',
      'src/App.test.tsx': "const color = '#fff'\nexport const x = <i className=\"only-in-test\" />",
      'src/__tests__/helper.ts': "export const c = 'rgb(1 2 3)'",
    },
    source: "import './app.css'",
  })
  assert.deepEqual(
    checkCss({ root }).map(({ file, rule }) => `${file} ${rule}`),
    ['src/app.css unused-class'],
  )
})

test('duplicate-selector separates @container and @layer contexts', (t) => {
  const root = fixture(t, {
    files: {
      'src/app.css': `
@container (min-width: 30em) { .tile { display: grid; } }
@container (min-width: 50em) { .tile { display: flex; } }
@layer base { .tile { margin: 0; } }
.tile { padding: 0; }
`,
    },
    source: "import './app.css'\nexport const tile = <i className=\"tile\" />",
  })
  assert.deepEqual(violationsFor(root, 'duplicate-selector'), [])
})

test('classes used through @apply are not unused', (t) => {
  const root = fixture(t, {
    files: {
      'src/index.css': `
.badge { display: inline-flex; }
.badge-green { @apply badge; }
.orphan { display: block; }
`,
    },
    source: "import './index.css'\nexport const b = <i className=\"badge-green\" />",
  })
  assert.deepEqual(
    violationsFor(root, 'unused-class').map((violation) => violation.message),
    ['Class ".orphan" is not referenced by src/**/*.{ts,tsx,js,jsx,mjs} or index.html.'],
  )
})
