#!/usr/bin/env bb
(ns workflow-ui-probe
  (:require [babashka.fs :as fs]
            [babashka.process :as process]))

(def script-dir (fs/parent (fs/absolutize *file*)))
(load-file (str (fs/path script-dir "workflow_state.bb")))

(defn put! [root relative body]
  (let [file (fs/path root relative)]
    (fs/create-dirs (fs/parent file))
    (spit (str file) body)))

(defn -main []
  (let [root (fs/create-temp-dir {:prefix "swarmforge-workflow-ui-"})
        now (str (java.time.Instant/now))]
    (put! root ".swarmforge/roles.tsv"
          (str "coder\tmaster\t" root "\tworkflow-ui-probe-coder\tCoder\tcodex\ttask\tforward-only\n"))
    ;; Intentionally recorded Done: the actual pending audit must keep this card
    ;; visible in the coder lane. No agent or tmux socket is created.
    (put! root ".swarmforge/board/tasks.tsv"
          (str "ui-probe\tdone\t" now "\t" now "\tui-probe-id\t1\n"))
    (put! root ".swarmforge/board/ui-probe.txt" "Manual workflow UI verification; no application task.\n")
    (put! root (str ".swarmforge/handoffs/audit_pending/"
                    (workflow-state/sha256 "coder") "/candidate.edn")
          (pr-str {:candidate {:sender "coder" :task-id "ui-probe-id" :task "ui-probe"}}))
    (put! root ".swarmforge/dashboard/clarifications/pending/ui-question.request"
          (str "id: ui-question\nstatus: pending\nrole: coder\ncreated_at: " now
               "\n\nManual UI check: enter a test answer. No agent is running, so delivery must fail visibly and the answer must remain available.\n"))
    (println "Manual UI probe; no models, agents, tmux sessions or downloads.")
    (println "Temporary data:" (str root))
    (println "Open the URL below in your existing browser.")
    (println "Stop this server with Ctrl+C, then observe the disconnected page without reloading it.")
    (flush)
    (process/exec "bb" (str (fs/path script-dir "pack_web.bb")) "--serve" (str root) "0")))

(when (= (str *file*) (System/getProperty "babashka.file"))
  (-main))
