import test from 'node:test';
import assert from 'node:assert/strict';
import { detectionMatchesCurrentCwd } from '../../static/js/launch-confirmation.js';

test('project detection unlocks only the inspected working directory', () => {
  assert.equal(detectionMatchesCurrentCwd(' D:\\site ', 'D:\\site', true), true);
  assert.equal(detectionMatchesCurrentCwd('d:/Site\\', 'D:\\site', true), true);
  assert.equal(detectionMatchesCurrentCwd('D:', 'D:\\', true), false);
  assert.equal(detectionMatchesCurrentCwd('D:\\other', 'D:\\site', true), false);
  assert.equal(detectionMatchesCurrentCwd('D:\\site', 'D:\\site', false), false);
  assert.equal(detectionMatchesCurrentCwd('', 'D:\\site', true), false);
  assert.equal(detectionMatchesCurrentCwd('   ', '   ', true), false);
  assert.equal(detectionMatchesCurrentCwd(null, null, true), false);
});
