// Behavior check for the rebuilt page (web/), run through the browser_clean MCP:
//   browser_run_code_unsafe with filename = this file, after navigating the page
//   to a fixture instance's /next/ (tests/browser/fixture.py data: ten Claude
//   sessions in dev/alpha, dev/beta and scratch, two Codex threads).
// Never point it at the real service: it clicks actions that change sessions.
// Returns { passed, failed: [...] } instead of throwing, so one run reports all.
async (page) => {
  const failed = []
  const passed = []
  const check = async (name, fn) => {
    try {
      await fn()
      passed.push(name)
    } catch (e) {
      failed.push(`${name}: ${String(e).split('\n')[0]}`)
    }
  }
  const expect = (cond, msg) => {
    if (!cond) throw new Error(msg)
  }
  const rows = () => page.locator('.session-list-rows .session-row')
  const search = page.getByRole('searchbox', { name: 'Search sessions' })
  const settle = () => page.waitForTimeout(250)

  await page.setViewportSize({ width: 375, height: 812 })
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForSelector('.session-row')

  await check('lists every session with no sideways scroll at 375', async () => {
    expect((await rows().count()) === 12, `rows: ${await rows().count()}`)
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
    expect(overflow === 0, `overflow ${overflow}px`)
  })

  await check('search filters, excludes and reports bad terms', async () => {
    await search.fill('alpha')
    await settle()
    expect((await rows().count()) === 5, `alpha: ${await rows().count()}`)
    await search.fill('-alpha -beta')
    await settle()
    expect((await rows().count()) === 3, `-alpha -beta: ${await rows().count()}`)
    await search.fill('is:codex')
    await settle()
    expect((await rows().count()) === 2, `is:codex: ${await rows().count()}`)
    await search.fill('is:nope')
    await settle()
    expect(await page.getByText('is: takes kept').isVisible(), 'no is: error notice')
    await search.fill('')
  })

  await check('typing offers transcript search on the reserved line', async () => {
    const before = await page.locator('.session-list-rows').boundingBox()
    await search.fill('reply')
    await settle()
    expect(await page.getByText('Search inside transcripts for “reply”').isVisible(), 'no offer')
    const after = await page.locator('.session-list-rows').boundingBox()
    expect(Math.abs(after.y - before.y) < 2, `rows moved ${after.y - before.y}px`)
  })

  await check('Enter switches to Transcripts and Back restores the query', async () => {
    await search.fill('alpha is:kept')
    await search.press('Enter')
    await page.getByText('Transcripts', { exact: true }).waitFor()
    expect(await page.getByText('Ignored inside transcripts: is:kept').isVisible(), 'no ignored note')
    await page.getByRole('button', { name: 'Sessions' }).click()
    await settle()
    expect((await search.inputValue()) === 'alpha is:kept', `query: ${await search.inputValue()}`)
    await search.fill('')
  })

  await check('folder sheet filters, Reset clears and nothing moves', async () => {
    const reset = page.getByRole('button', { name: 'Reset' })
    const resetBox = await reset.boundingBox()
    expect(await reset.isDisabled(), 'Reset enabled at defaults')
    await page.locator('.filter-folders').click()
    await page.getByRole('dialog').getByText('dev/beta').click()
    await page.getByRole('button', { name: 'Done' }).click()
    await settle()
    expect((await rows().count()) === 4, `beta: ${await rows().count()}`)
    const moved = await reset.boundingBox()
    expect(moved.x === resetBox.x && moved.y === resetBox.y, 'Reset moved')
    await reset.click()
    await settle()
    expect((await rows().count()) === 12, `after reset: ${await rows().count()}`)
  })

  await check('Show menu narrows to kept and the empty state offers Reset', async () => {
    await page.locator('.filter-show').click()
    await page.getByRole('menuitem', { name: 'Kept running' }).click()
    await settle()
    expect((await rows().count()) === 0, `kept: ${await rows().count()}`)
    await page.getByRole('button', { name: 'Reset filters' }).click()
    await settle()
    expect((await rows().count()) === 12, 'reset from empty state')
  })

  await check('tapping a title expands; only the checkbox selects', async () => {
    await page.locator('.session-title').first().click()
    await settle()
    expect((await page.locator('.session-details').count()) === 1, 'not expanded')
    expect((await page.locator('.session-row.is-selected').count()) === 0, 'expand selected it')
    await page.locator('.session-check input').nth(2).click()
    await settle()
    expect((await page.locator('.session-row.is-selected').count()) === 1, 'checkbox did not select')
    expect(await page.getByText('1 selected').isVisible(), 'no selection bar')
    await page.locator('.session-title').first().click()
  })

  await check('keyboard: j moves, Space selects, Escape clears', async () => {
    await page.setViewportSize({ width: 1280, height: 800 })
    await page.locator('body').click({ position: { x: 5, y: 400 } })
    await page.keyboard.press('Escape')
    await page.keyboard.press('j')
    await page.keyboard.press('j')
    await page.keyboard.press(' ')
    await settle()
    expect((await page.locator('.session-row.is-cursor').count()) === 1, 'no cursor')
    expect((await page.locator('.session-row.is-selected').count()) >= 1, 'space did not select')
    await page.keyboard.press('Escape')
    await settle()
    expect((await page.locator('.session-row.is-selected').count()) === 0, 'escape did not clear')
    await page.setViewportSize({ width: 375, height: 812 })
  })

  return { passed: passed.length, failed }
}
