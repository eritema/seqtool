import assert from 'node:assert/strict';
import test from 'node:test';

import { reverseComplement } from '../src/revcomp.mjs';

test('reverse complement reverses, complements, and preserves letter case [TEST-0004]', () => {
  assert.equal(reverseComplement('ACGTN'), 'NACGT');
  assert.equal(reverseComplement('aCgTn'), 'nAcGt');
  assert.equal(reverseComplement('AaCcGgTtNn'), 'nNaAcCgGtT');
  assert.equal(reverseComplement(''), '');
});

test('reverse complement rejects invalid bases and reports their positions [TEST-0005]', () => {
  assert.throws(() => reverseComplement('ACx'), /x.*posizione 3/i);
  assert.throws(() => reverseComplement('NN?'), /\?.*posizione 3/i);
});
