import { validateDnaSequence } from './dna.mjs';

const COMPLEMENT = {
  A: 'T',
  C: 'G',
  G: 'C',
  T: 'A',
  N: 'N',
  a: 't',
  c: 'g',
  g: 'c',
  t: 'a',
  n: 'n',
};

export function reverseComplement(seq) {
  validateDnaSequence(seq);

  const bases = [];
  for (let index = seq.length - 1; index >= 0; index -= 1) {
    bases.push(COMPLEMENT[seq[index]]);
  }

  return bases.join('');
}
