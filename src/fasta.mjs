import { validateDnaSequence } from './dna.mjs';

function lineError(lineNumber, message) {
  return new Error(`Errore alla linea ${lineNumber}: ${message}`);
}

export function parseFasta(text) {
  if (typeof text !== 'string') {
    throw new TypeError('Il testo FASTA deve essere una stringa.');
  }

  const records = [];
  const identifiers = new Set();
  let currentRecord = null;

  const finishRecord = () => {
    if (currentRecord.sequence.length === 0) {
      throw lineError(
        currentRecord.lineNumber,
        `il record "${currentRecord.id}" non contiene una sequenza.`,
      );
    }

    records.push({
      id: currentRecord.id,
      description: currentRecord.description,
      sequence: currentRecord.sequence,
    });
  };

  const lines = text.split(/\r\n|\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const lineNumber = index + 1;

    if (line.startsWith('>')) {
      if (currentRecord !== null) finishRecord();

      const header = line.slice(1).trim();
      const match = /^(\S+)(?:\s+(.*))?$/.exec(header);
      if (!match) {
        throw lineError(lineNumber, 'header FASTA senza identificativo.');
      }

      const [, id, description = ''] = match;
      if (identifiers.has(id)) {
        throw lineError(lineNumber, `identificativo duplicato "${id}".`);
      }

      identifiers.add(id);
      currentRecord = { id, description, sequence: '', lineNumber };
      continue;
    }

    if (line.trim().length === 0) continue;

    if (currentRecord === null) {
      throw lineError(lineNumber, 'testo prima del primo header FASTA.');
    }

    try {
      validateDnaSequence(line);
    } catch (error) {
      throw lineError(lineNumber, error.message);
    }

    currentRecord.sequence += line;
  }

  if (currentRecord !== null) finishRecord();
  return records;
}
