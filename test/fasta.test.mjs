import assert from 'node:assert/strict';
import test from 'node:test';

import { parseFasta } from '../src/fasta.mjs';

test('parseFasta keeps multiline records with their own identifiers [TEST-0006]', () => {
  const fasta = [
    '>sample-a alpha details',
    'ACG',
    '',
    'Tn',
    '>sample-b second sample',
    'GG',
    '>sample-c',
    'ac',
    '',
  ].join('\r\n');

  assert.deepEqual(parseFasta(fasta), [
    { id: 'sample-a', description: 'alpha details', sequence: 'ACGTn' },
    { id: 'sample-b', description: 'second sample', sequence: 'GG' },
    { id: 'sample-c', description: '', sequence: 'ac' },
  ]);
});

test('parseFasta reports line numbers for malformed input [TEST-0007]', () => {
  assert.throws(() => parseFasta('preamble\n>sample\nAC'), /linea 1/i);
  assert.throws(() => parseFasta('>empty\n\n>next\nA'), /linea 1/i);
  assert.throws(() => parseFasta('>sample\nA\n>sample\nC'), /linea 3/i);
  assert.throws(() => parseFasta('>sample\nAC?'), /linea 2/i);
});
