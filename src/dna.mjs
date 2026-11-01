const VALID_BASES = new Set(['A', 'C', 'G', 'T', 'N']);

export function validateDnaSequence(seq) {
  if (typeof seq !== 'string') {
    throw new TypeError('La sequenza DNA deve essere una stringa.');
  }

  let position = 0;
  for (const base of seq) {
    position += 1;
    if (!VALID_BASES.has(base.toUpperCase())) {
      throw new Error(
        `Carattere non valido "${base}" alla posizione ${position}; sono ammesse solo le basi A, C, G, T e N.`,
      );
    }
  }

  return seq.toUpperCase();
}
