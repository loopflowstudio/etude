import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

test('pilot and watcher share one safe table while Study stays participant-local', async ({
  context,
  page: pilot,
}) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await pilot.goto('/');
  await expect(pilot.getByTestId('connection-badge')).toHaveText('connected', {
    timeout: 15_000,
  });
  await pilot.getByTestId('opponent-select').selectOption('passive');
  await pilot.getByRole('button', { name: 'New Game' }).first().click();
  await expect(pilot.getByTestId('testing-house-panel')).toContainText(
    'You are the pilot',
  );

  await pilot.getByTestId('copy-watcher-invite').click();
  const invite = await pilot.evaluate(() => navigator.clipboard.readText());
  expect(invite).toContain('#table=');
  expect(invite).toContain('&watch=');

  const watcher = await context.newPage();
  await watcher.goto(invite);
  await expect(watcher.getByTestId('connection-badge')).toHaveText('connected', {
    timeout: 15_000,
  });
  await expect(watcher.getByTestId('testing-house-panel')).toContainText(
    'You are the watcher',
  );
  await expect(pilot.getByTestId('testing-house-panel')).toContainText(
    'both connected',
  );
  await expect(watcher.getByTestId('action-option').first()).toBeDisabled();
  await expect(watcher.getByRole('button', { name: 'Pilot only' })).toBeDisabled();
  const normalizedBoardText = async (target: typeof pilot): Promise<string> => (
    await target.getByTestId('game-board').innerText()
  ).replace(/\s+/g, ' ').trim();
  expect(await normalizedBoardText(watcher)).toBe(await normalizedBoardText(pilot));

  await watcher.getByTestId('author-belief').focus();
  await watcher.keyboard.press('Enter');
  await expect(watcher.getByTestId('testing-house-panel')).toContainText(
    'Your read · personal',
  );
  await expect(pilot.getByTestId('testing-house-panel')).not.toContainText(
    'Shared read',
  );
  await watcher.getByTestId('share-belief').focus();
  await watcher.keyboard.press('Space');
  await expect(pilot.getByTestId('testing-house-panel')).toContainText(
    'Shared read · table',
  );

  const pass = pilot.getByTestId('action-option').filter({ hasText: 'Pass priority' });
  await expect(pass.first()).toBeVisible();
  await pass.first().click();
  await expect(watcher.getByTestId('restore-table-decision').first()).toBeVisible();
  await watcher.getByTestId('restore-table-decision').first().click();
  await expect(watcher.getByTestId('participant-study-controls')).toBeVisible();
  await watcher.getByTestId('retry-table-decision').filter({ hasText: 'Pass priority' }).click();
  await expect(watcher.getByTestId('participant-study-controls')).toContainText(
    'Branch board shown only to you',
  );
  await expect(pilot.getByTestId('participant-study-controls')).toHaveCount(0);

  const pilotSeq = Number(await pilot.locator('main').getAttribute('data-update-seq'));
  await pilot.getByTestId('action-option').first().click();
  await expect.poll(async () => Number(
    await pilot.locator('main').getAttribute('data-update-seq'),
  )).toBeGreaterThan(pilotSeq);
  await expect(watcher.getByTestId('participant-study-controls')).toContainText(
    'Branch board shown only to you',
  );

  await watcher.getByTestId('return-from-participant-branch').click();
  await expect(watcher.getByTestId('participant-study-controls')).not.toContainText(
    'Branch board shown only to you',
  );
  await watcher.getByTestId('return-to-live-table').click();
  await expect(watcher.getByTestId('participant-study-controls')).toHaveCount(0);

  await pilot.getByTestId('transfer-pilot').focus();
  await pilot.keyboard.press('Enter');
  await expect(pilot.getByTestId('testing-house-panel')).toContainText(
    'You are the watcher',
  );
  await expect(watcher.getByTestId('testing-house-panel')).toContainText(
    'You are the pilot',
  );
  await expect(pilot.getByTestId('action-option').first()).toBeDisabled();
  await expect(watcher.getByTestId('action-option').first()).toBeEnabled();

  await watcher.setViewportSize({ width: 390, height: 844 });
  const overflows = await watcher.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(overflows).toBe(false);
  for (const button of await watcher.getByTestId('testing-house-panel').getByRole('button').all()) {
    const box = await button.boundingBox();
    if (box) expect(box.height).toBeGreaterThanOrEqual(44);
  }
  const accessibility = await new AxeBuilder({ page: watcher })
    .include('[data-testid="testing-house-panel"]')
    .analyze();
  expect(accessibility.violations).toEqual([]);

  await pilot.reload();
  await expect(pilot.getByTestId('connection-badge')).toHaveText('connected', {
    timeout: 15_000,
  });
  await expect(pilot.getByTestId('testing-house-panel')).toContainText(
    'You are the watcher',
  );
  await expect(watcher.getByTestId('testing-house-panel')).toContainText(
    'You are the pilot',
  );
});
