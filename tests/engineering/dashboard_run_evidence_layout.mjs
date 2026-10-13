import { expect } from './dashboard_coverage_fixture.mjs';

/** Check actual painted card geometry before capture, without changing it. */
async function measureRunEvidenceLayout(page) {
  const modal = page.locator('#promptHistoryDetailModal');
  const cards = await modal.isVisible() ? modal.locator('[data-run-evidence]') : page.locator('[data-run-evidence]');
  return cards.evaluateAll(nodes => nodes.map(node => {
    const rect = node.getBoundingClientRect();
    const heading = node.querySelector('h3').getBoundingClientRect();
    return { kind: node.dataset.runEvidence, x: rect.x, y: rect.y,
      right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height,
      scrollTop: node.scrollTop, heading: { x: heading.x, y: heading.y, bottom: heading.bottom },
      overflowY: getComputedStyle(node).overflowY };
  }));
}

function validateRunEvidenceLayout(layout) {
  if (layout.map(card => card.kind).join(',') !== 'selection,findings,publication')
    throw new Error('Run evidence requires all three real cards');
  for (const card of layout) {
    if (card.width <= 0 || card.height <= 0 || card.overflowY !== 'auto')
      throw new Error(`Run evidence ${card.kind} lost usable native scroll geometry`);
  }
  for (let index = 1; index < layout.length; index++) {
    if (layout[index].y < layout[index - 1].bottom)
      throw new Error(`Run evidence cards overlap: ${layout[index - 1].kind}/${layout[index].kind}`);
  }
}

export async function assertRunEvidenceLayout(page) {
  const layout = await measureRunEvidenceLayout(page);
  expect(() => validateRunEvidenceLayout(layout)).not.toThrow();
  return layout;
}

/** Prove the same assertion detects the original implicit-row defect. */
export async function assertHistoryLayoutRefault(page) {
  if (!await page.locator('#promptHistoryDetailModal').isVisible()) return;
  const css = await page.addStyleTag({ content:
    '#promptHistoryDetailContent { grid-auto-rows: auto; align-content: normal; }',
  });
  try {
    const original = await measureRunEvidenceLayout(page);
    expect(() => validateRunEvidenceLayout(original)).toThrow('Run evidence cards overlap');
  } finally {
    await css.evaluate(node => node.remove());
  }
  await assertRunEvidenceLayout(page);
}

/** Exercise the actual scroll container, then capture its readable heading. */
export async function readRunEvidenceCardFromTop(page, target) {
  await target.scrollIntoViewIfNeeded();
  await target.hover();
  const needsScroll = await target.evaluate(node => node.scrollHeight > node.clientHeight);
  if (needsScroll) {
    await page.mouse.wheel(0, 10000);
    await expect.poll(() => target.evaluate(node => node.scrollTop)).toBeGreaterThan(0);
  }
  await page.mouse.wheel(0, -10000);
  await expect.poll(() => target.evaluate(node => node.scrollTop)).toBe(0);
  const readable = await target.evaluate(node => {
    const card = node.getBoundingClientRect(), heading = node.querySelector('h3').getBoundingClientRect();
    return heading.y >= card.y && heading.bottom <= card.bottom;
  });
  expect(readable, 'Native scroll restores the readable card heading').toBe(true);
}
