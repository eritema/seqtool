# CRAP per JavaScript — kit locale

Kit importabile con `swarm_tool.sh`, con launcher `crap-js`, parser Acorn
8.15.0 incluso e licenza MIT conservata. Richiede Node 22 o successivo e
Bash. Nessun npm install o accesso di rete a runtime.

Stato: suite comportamentale eseguita dall'operatore con esito positivo dopo
le correzioni di contesto del runner e nomi CommonJS, il 22 ottobre 2026.
Non ancora validato sul viewer PDB. Non è un gate del workflow e non modifica
le regole di nessun progetto.

## Uso diretto

Dalla radice del progetto JavaScript da analizzare:

```bash
/percorso/swarm-forge/swarmforge/tool-kits/js-analysis/tools/crap-js/run \
  --source web/core -- node --test test/unit/*.test.js
```

Il comando esegue i test una volta, con raccolta V8 tramite
`NODE_V8_COVERAGE`. Il glob viene espanso dalla shell prima dell'avvio del
tool; il comando di test è eseguito senza una shell intermedia. Passare
argomenti distinti, non una singola stringa contenente il comando completo.
Il processo dei test è indipendente: il tool rimuove `NODE_TEST_CONTEXT`
dall'ambiente del comando figlio, preservando quello del chiamante. Questo
evita che un'invocazione dentro `node:test` venga scambiata per un test runner
ricorsivo e salti i file. Le funzioni assegnate a `exports.nome` o
`module.exports.nome` sono riportate con il nome statico della proprietà.

Ripetere `--source` per file o directory differenti. Sono selezionati `.js`,
`.mjs`, `.cjs`, ricorsivamente; esclusi `node_modules`, `.git`, `.swarmforge`.
I sorgenti devono stare nella worktree corrente e i symlink sono rifiutati.
Scegliere esplicitamente i sorgenti di produzione: il tool non deduce quali
cartelle siano test, dipendenze vendorizzate o codice generato.

Per una soglia esplicita, per esempio CRAP massimo 10:

```bash
.swarmforge/bin/crap-js --source web/core --max-crap 10 \
  -- node --test test/unit/*.test.js
```

Senza `--max-crap`, produce il report senza imporre una soglia. Il confronto
usa il punteggio completo, non quello arrotondato nella tabella. Questo exit
status diventa un gate solo se il progetto e il workflow lo usano come tale.

## Importazione nel progetto

Con helper SwarmForge aggiornato, dalla radice del progetto:

```bash
swarmforge/scripts/swarm_tool.sh import /percorso/js-analysis
swarmforge/scripts/swarm_tool.sh require crap-js
.swarmforge/bin/crap-js --source web/core -- node --test test/unit/*.test.js
```

Il nome `crap-js` è già nel catalogo. Import abilita la policy offline dei
tool e crea i launcher; non sovrascrive un tool locale già presente. Il
parser è relativo al launcher e il kit rimane utilizzabile dopo un trasferimento.

Nel container PDB il sorgente fidato host non è montato. Dopo aver verificato
il kit, per la sua prima introduzione nel progetto:

```bash
mkdir -p /Users/raffo/github/swarmforge-esempi/pdb-viewer-locale/swarmforge/tool-kits
cp -R /Users/raffo/github/swarm-forge/swarmforge/tool-kits/js-analysis \
  /Users/raffo/github/swarmforge-esempi/pdb-viewer-locale/swarmforge/tool-kits/
```

Non ripetere la copia su una versione già presente senza pianificarne
l'aggiornamento. Importare poi nel worker, usando il suo nome effettivo:

```bash
docker exec -w /workspace swarmforge-845c062b73df7ea7-worker \
  ./swarmforge/scripts/swarm_tool.sh import /workspace/swarmforge/tool-kits/js-analysis

docker exec -w /workspace swarmforge-845c062b73df7ea7-worker sh -c \
  '.swarmforge/bin/crap-js --source web/core -- node --test test/unit/*.test.js'
```

Sono comandi per l'operatore, non eseguiti durante la preparazione. Nel viewer
le regole esentano attualmente CRAP: per renderlo obbligatorio al cleaner
occorre una modifica esplicita di `project.prompt`, propagata alle worktree.
Non eseguire tutte le procedure di questa pagina come una checklist.

Per attivare l'obbligo di review CRAP nel two-pack PDB usare la
[procedura dedicata](../../../docs/tutorial/README.md#facoltativo-crap-sul-javascript), con
profilo approvato delle regole, task iniziale di baseline e flow-chart delle
decisioni del cleaner. Non richiede di modificare i prompt generali dei ruoli.

## Metrica e convenzioni

Per ogni funzione e per l'eventuale inizializzazione `<module>`:

```text
CRAP = CC² × (1 − copertura)³ + CC
```

La copertura è una frazione tra 0 e 1. Esempi: CC=5 e copertura zero danno
30; CC=5 e copertura completa danno 5; CC=4 e copertura 50% danno 6.

### Complessità

Calcolata sull'AST, senza contare parole o simboli con regex. Ogni funzione
parte da 1. Incrementi per `if`, ternario, cicli `for`, `for-in`, `for-of`,
`while`, `do-while`, `catch`, casi switch non default, operatori logici
`&&`, `||`, `??`, assegnazioni logiche, valori di default e collegamenti
opzionali `?.`. Le funzioni annidate hanno il proprio punteggio e non
aumentano quello della funzione esterna. `<module>` è un'unità di
inizializzazione separata, non una funzione inventata nel sorgente.

### Copertura per punti di esecuzione AST

La formula usa una convenzione dichiarata: statements, dichiarazioni di
variabili/classi e ingressi delle espressioni dei rami ternari, cortocircuiti
e default. Blocchi vuoti, dichiarazioni di funzioni e direttive non sono
statements eseguibili conteggiati. Le arrow concise includono l'espressione
del corpo; per funzioni vuote si misura l'ingresso del corpo. Il report
espone punti coperti, punti totali e frazione.

Ogni punto è verificato contro i contatori delle funzioni e dei blocchi V8,
rispettando gli intervalli annidati. È coperto se risulta eseguito in almeno
uno dei processi/snapshot correnti; non si mediano le percentuali dei worker.
Le funzioni annidate non gonfiano la coverage della funzione esterna.

Questa non è la percentuale di righe stampata da Node né necessariamente la
coverage Istanbul o di altri strumenti CRAP. Il JSON conserva la convenzione:
non confrontare soglie di strumenti differenti come se la metrica fosse identica.

Un sorgente selezionato ma mai caricato ha coverage zero con evidenza
`source-not-loaded`, anziché sparire dal report. Un sorgente caricato ma non
mappabile produce errore. Contatori solo per funzione non sono presentati
come coverage precisa dei blocchi quando la funzione è stata eseguita.

## Report ed errori

Ogni invocazione crea una directory nuova sotto
`.swarmforge/reports/crap-js/run-*`, oppure sotto `--output DIR`:

- `manifest.json`: comando, Node, timestamp e SHA256 dei sorgenti.
- `v8/`: snapshot originali della misurazione corrente.
- `report.json`: risultati per unità ordinati per rischio decrescente,
  convenzioni, punteggi completi e superamenti della soglia.
- `failure.json`: dettagli dell'uscita quando il comando di test fallisce.

Non c'è riuso automatico di vecchia coverage. Gli hash sono ricontrollati
dopo i test; una modifica dei sorgenti invalida la misurazione. Controllare
l'esito della stessa invocazione, non aprire un vecchio report dopo un errore.
Il tool conserva le evidenze già prodotte e non cancella i run precedenti.
I report possono contenere percorsi e nomi del progetto.

| Uscita | Significato |
|---|---|
| 0 | Analisi completata; eventuale soglia rispettata |
| 1 | Analisi completata, soglia superata |
| 2 | Parametri, parsing, sorgenti o coverage non validi |
| 3 | Comando di test fallito o terminato da segnale; nessun report di successo |

## Limiti di questo incremento

- JavaScript ECMAScript 2022 non trasformato. Niente TypeScript, JSX,
  transpiler, loader o rimappatura tramite source map.
- Campi di classe e blocchi statici sono rifiutati: non si ignora la loro
  complessità implicita per produrre un report parziale. I normali metodi
  di classe sono funzioni analizzabili.
- Solo codice eseguito dai processi Node del comando scelto. Codice browser
  non importato resta a zero; non dimostra rendering WebGL o correttezza shader.
- Richiede corrispondenza degli offset V8 e AST: una forma non mappabile
  provoca errore, non una stima. Non importa coverage LCOV o percentuali
  per file per attribuirle arbitrariamente alle funzioni.
- Non misura qualità delle asserzioni o mutation score. Un CRAP basso
  non garantisce correttezza funzionale.
- Il comando dei test è codice del progetto eseguito con i privilegi del
  tool. La policy offline non è una sandbox: usare il worker isolato come
  per gli altri test. Il kit non installa strumenti o apre connessioni.

## Verifica del kit a carico dell'operatore

Dalla repository SwarmForge, una volta per questa implementazione:

```bash
node --test test/tool-kits/crap-js.test.mjs
```

La suite usa fixture temporanee, Node/Babashka e helper locali: niente Docker,
modelli o download. Verifica formula, AST, coverage reale, unione dei worker,
codice non caricato, mancato riuso dei dati, fallimenti, sorgenti cambiati e
trasferibilità. Nessuna asserzione sul testo dei prompt. Non è una suite da
ripetere a ogni task PDB.

## Fonti e provenienza

- [Formula CRAP: riferimento crap4clj](https://github.com/unclebob/crap4clj).
- [Node: NODE_V8_COVERAGE](https://nodejs.org/docs/latest-v22.x/api/cli.html#node_v8_coveragedir).
- [V8: JavaScript code coverage](https://v8.dev/blog/javascript-code-coverage).
- [Complessità JavaScript: ESLint](https://eslint.org/docs/latest/rules/complexity).
- [Parser Acorn](https://github.com/acornjs/acorn), versione 8.15.0.

`tools/crap-js/vendor/provenance.json` registra URL, integrità SHA512
dell'archivio npm e SHA256 dei file copiati. L'archivio è stato verificato
prima dell'estrazione, senza eseguirne script. Licenza originale in
`tools/crap-js/vendor/ACORN-LICENSE`. Non è incluso l'intero node_modules.
