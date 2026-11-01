(ns recovery-control
  (:require [babashka.fs :as fs]
            [clojure.string :as str])
  (:import [java.nio.channels FileChannel]
           [java.nio.file OpenOption StandardOpenOption]))

;; Self-contained control-plane entry point, streamed by the trusted host
;; controller and evaluated inside the worker. Never load project runtime code.
;; Use exactly the same role lock and control-file protocol as the watchdog.
(def lock-options
  (into-array OpenOption [StandardOpenOption/CREATE StandardOpenOption/WRITE]))

(defn configured-role! [root role]
  (when-not (and root (re-matches #"[A-Za-z0-9-]+" (or role "")))
    (throw (ex-info "Invalid recovery role." {})))
  (let [roles (fs/path root ".swarmforge/roles.tsv")]
    (when-not (and (fs/regular-file? roles)
                   (some #(= role (first (str/split % #"\t")))
                         (str/split-lines (slurp (str roles)))))
      (throw (ex-info "Recovery control requires a configured role in an existing swarm." {})))))

(defn try-lock [channel]
  (try
    (.tryLock channel)
    (catch Exception e
      (if (= "java.nio.channels.OverlappingFileLockException" (.getName (class e)))
        nil
        (throw e)))))

(defn set-paused! [root role paused?]
  (configured-role! root role)
  (let [agents (fs/path root ".swarmforge/agents")
        lock-file (fs/path agents (str role ".lock"))
        file (fs/path agents "recovery-control" (str role ".edn"))]
    (fs/create-dirs agents)
    (with-open [channel (FileChannel/open lock-file lock-options)]
      (when-not (try-lock channel)
        (throw (ex-info "Role coordination is already in progress; retry after it finishes."
                        {:code "AGENT_COORDINATION_BUSY" :http-status 409})))
      (fs/create-dirs (fs/parent file))
      (let [tmp (fs/create-temp-file {:dir (fs/parent file) :prefix ".recovery-"})]
        (spit (str tmp) (pr-str {:paused (boolean paused?) :changed-at (System/currentTimeMillis)}))
        (fs/move tmp file {:replace-existing true})))
    (println (if paused? "RECOVERY_PAUSED:" "RECOVERY_RESUMED:") role
             "Task files and recovery budget were retained. Running turns were not interrupted.")))

(defn -main [& args]
  (when-not (and (= 3 (count args)) (#{"pause-recovery" "resume-recovery"} (nth args 2)))
    (throw (ex-info "Usage: recovery-control ROOT ROLE pause-recovery|resume-recovery" {})))
  (let [[root role action] args]
    (set-paused! root role (= "pause-recovery" action))))
