// Repo configuration and the command line shared by check-css.mjs and
// check-structure.mjs. Settings live in blocks of .codeshape/config.json
// (`css`, `structure`); consolidate's drift_check.py reads only its own keys
// from the same file, so the blocks coexist.
import { existsSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

export function readCodeshapeConfig(root, path) {
  const file = path ? resolve(path) : join(root, '.codeshape/config.json')
  if (!existsSync(file)) {
    if (path) throw new Error(`Config file not found: ${file}`)
    return {}
  }
  return JSON.parse(readFileSync(file, 'utf8'))
}

export function configBlock(config, name, defaults) {
  const block = config?.[name] ?? {}
  const unknown = Object.keys(block).filter((key) => !(key in defaults))
  if (unknown.length) {
    throw new Error(
      `Unknown ${name} setting(s) in .codeshape/config.json: ${unknown.join(', ')}`,
    )
  }
  return { ...defaults, ...block }
}

function cliOptions(argv, defaultRoot) {
  const options = { config: undefined, json: false, root: defaultRoot }
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--root') options.root = resolve(argv[(index += 1)])
    else if (argv[index] === '--config') options.config = argv[(index += 1)]
    else if (argv[index] === '--json') options.json = true
    else throw new Error(`Unknown argument: ${argv[index]}`)
  }
  return options
}

// Runs `check({ root, config })` for a script's command line:
// [--root <repo>] [--config <file shaped like .codeshape/config.json>] [--json]
export function runCheckCli({ argv, check, defaultRoot, label }) {
  const options = cliOptions(argv, defaultRoot)
  const config = readCodeshapeConfig(options.root, options.config)
  const violations = check({ root: options.root, config })
  if (options.json) {
    console.log(JSON.stringify(violations, null, 2))
  } else if (violations.length) {
    for (const violation of violations) {
      console.error(
        `${violation.file}:${violation.line} ${violation.rule} ${violation.message}`,
      )
    }
  } else {
    console.log(`${label} OK`)
  }
  if (violations.length) process.exitCode = 1
}

export function sortViolations(violations) {
  return violations.sort(
    (left, right) =>
      left.file.localeCompare(right.file) ||
      left.line - right.line ||
      left.rule.localeCompare(right.rule) ||
      left.message.localeCompare(right.message),
  )
}
