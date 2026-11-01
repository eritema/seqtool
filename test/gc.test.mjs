import assert from 'node:assert/strict';
import test from 'node:test';

import { gcContent } from '../src/gc.mjs';
import { validateDnaSequence } from '../src/dna.mjs';

test('GC fraction counts C and G and excludes N [TEST-0001]', () => {
  assert.equal(gcContent('gCnAtN'), 0.5);
  assert.equal(gcContent('GGcc'), 1);
  assert.equal(gcContent('aAtT'), 0);
});

test('GC fraction rejects sequences without informative bases [TEST-0002]', () => {
  assert.throws(() => gcContent(''), /almeno una base informativa/i);
  assert.throws(() => gcContent('NNn'), /almeno una base informativa/i);
});

test('DNA validation rejects invalid characters and reports their position [TEST-0003]', () => {
  assert.equal(validateDnaSequence('acgtnN'), 'ACGTNN');
  assert.throws(() => validateDnaSequence('acXgt'), /X.*posizione 3/i);
  assert.throws(() => gcContent('acXgt'), /X.*posizione 3/i);
  assert.throws(() => gcContent('ACGT!'), /!.*posizione 5/i);
  assert.throws(() => gcContent('NN?'), /\?.*posizione 3/i);
  for (const seq of [null, undefined, 42, ['A', 'C'], { length: 2 }]) {
    assert.throws(() => validateDnaSequence(seq), TypeError);
    assert.throws(() => gcContent(seq), TypeError);
  }
});
