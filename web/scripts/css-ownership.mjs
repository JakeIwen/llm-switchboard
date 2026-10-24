// Stylesheet ownership: which modules may use a class and which sheet must
// hold it. Global-layer sheets (under `globalStyles`) are shared by everyone;
// every other sheet belongs to the modules that import it.

function addToSetMap(map, key, value) {
  const values = map.get(key) ?? new Set()
  values.add(value)
  map.set(key, values)
}

function uniqueItems(items, keyFor) {
  const seen = new Set()
  return items.filter((item) => {
    const key = keyFor(item)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function ownershipIndex(stylesheets, modules, globalStyles) {
  const globalClasses = new Set()
  const componentSheetsByClass = new Map()
  const importersByStylesheet = new Map()
  for (const stylesheet of stylesheets) {
    for (const item of stylesheet.classes) {
      if (stylesheet.file.startsWith(globalStyles)) {
        globalClasses.add(item.className)
      } else {
        addToSetMap(componentSheetsByClass, item.className, stylesheet.file)
      }
    }
  }
  for (const module of modules) {
    for (const stylesheet of module.imports) {
      addToSetMap(importersByStylesheet, stylesheet, module.file)
    }
  }
  return { componentSheetsByClass, globalClasses, importersByStylesheet }
}

function moduleReferencesByClass(modules) {
  const references = new Map()
  for (const module of modules) {
    for (const reference of module.references) {
      addToSetMap(references, reference.className, module.file)
    }
  }
  return references
}

function colocateViolations(stylesheets, modules, indexReferences, globalStyles) {
  const references = moduleReferencesByClass(modules)
  const indexClasses = new Set(indexReferences.map((item) => item.className))
  const occurrences = stylesheets
    .filter(({ file }) => file.startsWith(globalStyles))
    .flatMap(({ classes }) => classes)
  return uniqueItems(
    occurrences,
    (item) => `${item.file}\0${item.line}\0${item.className}`,
  ).flatMap((item) => {
    const moduleFiles = references.get(item.className) ?? new Set()
    if (
      item.exempt ||
      indexClasses.has(item.className) ||
      moduleFiles.size !== 1
    ) {
      return []
    }
    const [moduleFile] = moduleFiles
    return [
      {
        file: item.file,
        line: item.line,
        rule: 'colocate',
        message: `Class ".${item.className}" is only used by ${moduleFile}; move it to a stylesheet beside it.`,
      },
    ]
  })
}

function componentDefinitionMessage(className, sheets, moduleFile) {
  if (sheets.size === 1) {
    const [stylesheet] = sheets
    return `Class ".${className}" is defined in ${stylesheet}, but that stylesheet is not imported by ${moduleFile}.`
  }
  return `Class ".${className}" is defined in component stylesheets, but none are imported by ${moduleFile}.`
}

function ownershipViolations(modules, index) {
  const violations = []
  for (const module of modules) {
    const imports = new Set(module.imports)
    const references = uniqueItems(
      module.references,
      (item) => `${item.line}\0${item.className}`,
    )
    for (const reference of references) {
      const sheets = index.componentSheetsByClass.get(reference.className)
      if (
        reference.exempt ||
        index.globalClasses.has(reference.className) ||
        !sheets ||
        [...sheets].some((sheet) => imports.has(sheet))
      ) {
        continue
      }
      violations.push({
        file: module.file,
        line: reference.line,
        rule: 'ownership',
        message: componentDefinitionMessage(
          reference.className,
          sheets,
          module.file,
        ),
      })
    }
  }
  return violations
}

function importerReferences(modules) {
  return new Map(
    modules.map((module) => [
      module.file,
      new Set(module.references.map((item) => item.className)),
    ]),
  )
}

function couplingViolations(stylesheets, modules, index, globalStyles) {
  const references = importerReferences(modules)
  const referencedClasses = new Set(
    modules.flatMap((module) =>
      module.references.map((item) => item.className),
    ),
  )
  const violations = []
  for (const stylesheet of stylesheets) {
    if (stylesheet.file.startsWith(globalStyles)) continue
    const importers =
      index.importersByStylesheet.get(stylesheet.file) ?? new Set()
    const classes = uniqueItems(
      stylesheet.classes,
      (item) => `${item.line}\0${item.className}`,
    )
    for (const item of classes) {
      const usedByImporter = [...importers].some((file) =>
        references.get(file)?.has(item.className),
      )
      if (
        item.exempt ||
        !referencedClasses.has(item.className) ||
        index.globalClasses.has(item.className) ||
        usedByImporter
      ) {
        continue
      }
      violations.push({
        file: stylesheet.file,
        line: item.line,
        rule: 'coupling',
        message: `Class ".${item.className}" is not referenced by any module that imports ${stylesheet.file}.`,
      })
    }
  }
  return violations
}

function reachableStylesheets(stylesheets, modules) {
  const importsByStylesheet = new Map(
    stylesheets.map((stylesheet) => [stylesheet.file, stylesheet.imports]),
  )
  const reachable = new Set(modules.flatMap((module) => module.imports))
  const pending = [...reachable]
  while (pending.length) {
    const stylesheet = pending.pop()
    for (const imported of importsByStylesheet.get(stylesheet) ?? []) {
      if (reachable.has(imported)) continue
      reachable.add(imported)
      pending.push(imported)
    }
  }
  return reachable
}

function orphanViolations(stylesheets, modules) {
  const reachable = reachableStylesheets(stylesheets, modules)
  return stylesheets.flatMap((stylesheet) => {
    if (reachable.has(stylesheet.file)) return []
    return [
      {
        file: stylesheet.file,
        line: 1,
        rule: 'orphan-stylesheet',
        message: `Stylesheet "${stylesheet.file}" is not reachable from a relative import in a script module.`,
      },
    ]
  })
}

export function checkCssOwnership({
  globalStyles,
  indexReferences,
  modules,
  stylesheets,
}) {
  const index = ownershipIndex(stylesheets, modules, globalStyles)
  return [
    ...colocateViolations(stylesheets, modules, indexReferences, globalStyles),
    ...ownershipViolations(modules, index),
    ...couplingViolations(stylesheets, modules, index, globalStyles),
    ...orphanViolations(stylesheets, modules),
  ]
}
