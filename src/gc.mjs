import { validateDnaSequence } from './dna.mjs';

export function gcContent(seq) {
  const normalizedSequence = validateDnaSequence(seq);
  let gcBases = 0;
  let informativeBases = 0;

  for (const base of normalizedSequence) {
    if (base === 'N') continue;

    informativeBases += 1;
    if (base === 'G' || base === 'C') gcBases += 1;
  }

  if (informativeBases === 0) {
    throw new RangeError(
      'La sequenza deve contenere almeno una base informativa (A, C, G o T).',
    );
  }

  return gcBases / informativeBases;
}
