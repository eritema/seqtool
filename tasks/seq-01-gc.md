# seq-01-gc

Realizza URS-0001. Implementa gcContent(seq) in src/gc.mjs: restituisce la frazione di G e C sul totale delle basi, escluse le N. Una sequenza vuota, o composta solo da N, è un errore. Un carattere non valido è un errore che indica carattere e posizione. La validazione delle basi sta in src/dna.mjs, riusabile. Con hammer crea le SRS derivate da URS-0001, l'ARCH, le UNIT e i TEST collegati. Rischio: un valore errato restituito senza errore porta a una valutazione sbagliata del campione. Crea il RISK, la misura di controllo e il test che la verifica.
