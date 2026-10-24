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

  // Actions against the fixture. Each check undoes what it did, so a run leaves
  // the fixture as it found it.
  const dialog = () => page.getByRole('dialog')
  const rowNamed = (title) => page.locator('.session-list-rows .session-row', { hasText: title })
  const waitPoll = () => page.waitForTimeout(1500)

  await check('keep switch turns on, then off (asking first only when busy)', async () => {
    const row = rowNamed('Beta perf profiling')
    await row.getByRole('switch', { name: 'Keep running' }).click({ force: true })
    await waitPoll()
    expect(await row.evaluate((el) => el.classList.contains('is-kept')), 'not kept after switching on')
    await row.getByRole('switch', { name: 'Keep running' }).click({ force: true })
    await settle()
    if (await dialog().count()) await dialog().getByRole('button', { name: 'Stop' }).click()
    await waitPoll()
    expect(!(await row.evaluate((el) => el.classList.contains('is-kept'))), 'still kept after switching off')
  })

  await check('rename through its form, then back', async () => {
    const row = rowNamed('Beta release notes')
    await row.locator('.session-title').click()
    await row.getByRole('button', { name: 'Rename…' }).click()
    const title = dialog().getByRole('textbox', { name: 'Title' })
    await title.fill('Beta release notes v2')
    await dialog().getByRole('button', { name: 'Rename' }).click()
    await waitPoll()
    expect((await rowNamed('Beta release notes v2').count()) === 1, 'title did not change')
    await rowNamed('Beta release notes v2').getByRole('button', { name: 'Rename…' }).click()
    await dialog().getByRole('textbox', { name: 'Title' }).fill('Beta release notes')
    await dialog().getByRole('button', { name: 'Rename' }).click()
    await waitPoll()
    expect((await rowNamed('Beta release notes v2').count()) === 0, 'title did not change back')
    await rowNamed('Beta release notes').locator('.session-title').click()
  })

  await check('delete asks, then Undo restores', async () => {
    const before = await rows().count()
    const row = rowNamed('Quick question about systemd')
    await row.locator('.session-title').click()
    await row.getByRole('button', { name: 'Delete' }).click()
    await dialog().getByRole('button', { name: 'Delete' }).click()
    await waitPoll()
    expect((await rows().count()) === before - 1, 'row not removed')
    expect(await page.getByText('Recently deleted (1)').isVisible(), 'not in Recently deleted')
    await page.locator('.toast').getByRole('button', { name: 'Undo' }).click()
    await waitPoll()
    expect((await rows().count()) === before, 'Undo did not restore')
  })

  await check('selection bar keeps two running, then stops them', async () => {
    const a = rowNamed('Alpha CAN decoder')
    const b = rowNamed('Alpha telemetry export')
    await a.locator('.session-check input').click()
    await b.locator('.session-check input').click()
    await page.locator('.bulkbar').getByRole('button', { name: 'Keep running' }).click()
    await waitPoll()
    expect(await a.evaluate((el) => el.classList.contains('is-kept')), 'first not kept')
    expect(await b.evaluate((el) => el.classList.contains('is-kept')), 'second not kept')
    await page.locator('.bulkbar').getByRole('button', { name: 'More selection actions' }).click()
    await page.getByRole('menuitem', { name: 'Stop keeping' }).click()
    const confirm = dialog().getByRole('button', { name: /Stop/ })
    if (await confirm.count()) await confirm.click()
    await waitPoll()
    expect(!(await a.evaluate((el) => el.classList.contains('is-kept'))), 'first still kept')
    await page.locator('.bulkbar').getByRole('button', { name: 'Clear selection' }).click()
  })

  await check('Codex rows offer Fork, not Move', async () => {
    const row = rowNamed('Codex alpha refactor')
    await row.locator('.session-title').click()
    expect((await row.getByRole('button', { name: 'Move…' }).count()) === 0, 'Codex row offers Move')
    await row.getByRole('button', { name: 'Fork…' }).click()
    await dialog().waitFor({ timeout: 3000 })
    await dialog().getByRole('button', { name: 'Cancel' }).click()
    await row.locator('.session-title').click()
  })

  await check('settings open with the install values and cancel cleanly', async () => {
    await page.getByRole('button', { name: 'Settings' }).click()
    await dialog().getByRole('textbox', { name: /Claude command/ }).waitFor()
    await dialog().getByRole('button', { name: 'Cancel' }).click()
    await dialog().waitFor({ state: 'detached', timeout: 3000 })
  })

  await check('new session form starts one in a listed folder; delete removes it', async () => {
    const before = await rows().count()
    await page.getByRole('button', { name: 'New' }).click()
    await dialog().getByRole('textbox', { name: 'Name' }).fill('Browser check session')
    await dialog().getByRole('button', { name: /Start|Create/ }).click()
    await waitPoll()
    const row = rowNamed('Browser check session')
    expect((await row.count()) === 1, `no new row (${await rows().count()} vs ${before})`)
    await row.locator('.session-title').click()
    await row.getByRole('button', { name: 'Delete' }).click()
    await dialog().getByRole('button', { name: 'Delete' }).click()
    await waitPoll()
    expect((await rowNamed('Browser check session').count()) === 0, 'new session not deleted')
  })

  return { passed: passed.length, failed }
}
