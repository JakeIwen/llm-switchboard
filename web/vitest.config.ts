import { configDefaults, defineConfig } from 'vitest/config'

// Pure modules only; components are checked in the browser (tests/browser).
// scripts/ holds the react-app checks' node:test files, run by test:react-app.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    exclude: [...configDefaults.exclude, 'scripts/**'],
  },
})
