# seq-03-fasta

Realizza URS-0003. Implementa parseFasta(text) in src/fasta.mjs. Restituisce [{id, description, sequence}], dove id è la prima parola dopo > e description è il resto della riga. Le righe di sequenza vanno unite; le righe vuote e i CRLF sono ammessi. Sono errori, ciascuno con il numero di riga: testo prima del primo >, un record senza sequenza, un identificativo duplicato, un carattere non valido. Rischio: record fusi o persi associano una sequenza al campione sbagliato. Crea RISK, misura e test con hammer.
