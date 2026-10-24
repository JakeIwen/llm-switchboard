// Walking a repo's source folders for the react-app checks.
import { existsSync, readdirSync } from 'node:fs'
import { extname, join, relative, sep } from 'node:path'

const SKIPPED_DIRECTORIES = new Set(['node_modules', 'dist', 'build'])
const TEST_FILE = /(?:^|\/)__tests__\/|\.(?:test|spec)\.[cm]?[jt]sx?$/

export function relativePath(root, path) {
  return relative(root, path).split(sep).join('/')
}

export function filesUnder(directory, extensions) {
  if (!existsSync(directory)) return []
  return readdirSync(directory, { withFileTypes: true })
    .flatMap((entry) => {
      const path = join(directory, entry.name)
      if (entry.isDirectory()) {
        if (SKIPPED_DIRECTORIES.has(entry.name) || entry.name.startsWith('.')) {
          return []
        }
        return filesUnder(path, extensions)
      }
      return entry.isFile() && extensions.has(extname(entry.name)) ? [path] : []
    })
    .sort()
}

// `exclude` entries are path prefixes relative to the repo root. Test files
// are always skipped: they import components and name classes without being
// part of the rendered app.
export function sourceFiles(root, sourceRoots, extensions, exclude = []) {
  const wanted = new Set(extensions)
  return sourceRoots
    .flatMap((sourceRoot) => filesUnder(join(root, sourceRoot), wanted))
    .filter((path) => {
      const file = relativePath(root, path)
      return !TEST_FILE.test(file) && !exclude.some((prefix) => file.startsWith(prefix))
    })
}
