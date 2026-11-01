#!/usr/bin/env bb
(ns swarm-startup
  (:require [babashka.fs :as fs]
            [clojure.edn :as edn]
            [clojure.java.shell :as shell]
            [clojure.string :as str]))

(def helper-names
  ["ready_for_next.sh" "done_with_current.sh" "swarm_handoff.sh" "swarm_audit.sh"
   "swarm_tool.sh" "pack_dashboard_request.sh"])

(def aps-tools #{"gherkin-parser" "ir-dry-checker" "gherkin-mutator"})

(defn policy [root]
  (let [file (fs/path root "swarmforge/startup.edn")
        value (if (fs/exists? file) (edn/read-string (slurp (str file))) {})]
    (when-not (and (map? value)
                   (every? #{:aps-enabled} (keys value))
                   (or (not (contains? value :aps-enabled))
                       (boolean? (:aps-enabled value))))
      (throw (ex-info (str "STARTUP_CONFIG_INVALID: " file
                           " must contain only :aps-enabled true or false") {})))
    (merge {:aps-enabled true} value)))

(defn protected-runtime? []
  (fs/regular-file? "/opt/swarmforge/runtime/runtime.edn"))

(defn helper-dir [root worktree]
  (if (protected-runtime?)
    (fs/path "/opt/swarmforge/runtime/swarmforge/scripts")
    (fs/path worktree "swarmforge/scripts")))

(defn role-path [root worktree inherited]
  (let [tools (fs/path root ".swarmforge/bin")
        helpers (helper-dir root worktree)
        paths (if (protected-runtime?) [helpers tools] [tools helpers])]
    (str (str/join ":" (map str paths))
         (when-not (str/blank? inherited) (str ":" inherited)))))

(defn state-file [root role]
  (when-not (re-matches #"[A-Za-z0-9_-]+" (or role ""))
    (throw (ex-info "STARTUP_ROLE_INVALID" {})))
  (fs/path root ".swarmforge/startup" (str role ".edn")))

(defn read-state [root role]
  (let [file (state-file root role)]
    (when (fs/regular-file? file)
      (try (edn/read-string (slurp (str file)))
           (catch Exception _ {:status :failed :message "Unreadable startup state; rerun startup check."})))))

(defn write-state! [root role state]
  (let [file (state-file root role)]
    (fs/create-dirs (fs/parent file))
    (let [tmp (fs/create-temp-file {:dir (fs/parent file) :prefix ".startup-"})]
      (spit (str tmp) (pr-str state))
      (fs/move tmp file {:replace-existing true}))))

(defn resolved [command]
  ;; Names are passed as positional arguments, never interpolated as shell code.
  (let [result (shell/sh "/bin/sh" "-c" "command -v \"$1\"" "startup" command)]
    (when (zero? (:exit result)) (not-empty (str/trim (:out result))))))

(defn same-file? [a b]
  (try (= (fs/real-path a) (fs/real-path b)) (catch Exception _ false)))

(defn git-value [directory & args]
  (let [result (apply shell/sh (concat ["git" "-C" (str directory)] args))]
    (when-not (zero? (:exit result))
      (throw (ex-info (str "Git layout check failed: " (str/trim (:err result))) {})))
    (str/trim (:out result))))

(defn git-common-dir [directory]
  (let [path (fs/path (git-value directory "rev-parse" "--git-common-dir"))]
    (if (fs/absolute? path) path (fs/path directory path))))

(defn git-layout-failures [root worktree]
  (try
    (let [top (git-value worktree "rev-parse" "--show-toplevel")]
      (vec (concat
             (when-not (same-file? top worktree)
               [(str "Assigned directory is not a Git worktree root; Git resolves to " top)])
             (when-not (same-file? (git-common-dir root) (git-common-dir worktree))
               ["Assigned worktree belongs to a different Git repository"])
             (when-not (same-file? root worktree)
               (let [expected (str "swarmforge-" (fs/file-name worktree))
                     actual (git-value worktree "symbolic-ref" "--quiet" "--short" "HEAD")]
                 (when-not (= expected actual)
                   [(str "Wrong worktree branch; expected " expected ", got " actual)]))))))
    (catch Exception e [(ex-message e)])))

(defn inspect [root role worktree agent]
  (policy root)
  (let [scripts (helper-dir root worktree)
        failures (vec
                  (concat
                   (when-not (= role (System/getenv "SWARMFORGE_ROLE"))
                     ["SWARMFORGE_ROLE does not match the assigned role"])
                   (when-not (same-file? worktree (fs/cwd))
                     [(str "Wrong directory; expected " worktree)])
                   (git-layout-failures root worktree)
                   (for [name (concat ["bb" "git" "zsh" agent] helper-names)
                         :let [found (resolved name)]
                         :when (or (nil? found)
                                   (and (some #{name} helper-names)
                                        (not (and (fs/executable? (fs/path scripts name))
                                                  (same-file? found (fs/path scripts name))))))]
                     (str name ": " (or found "not on PATH")
                          (when (some #{name} helper-names)
                            (str "; expected " (fs/path scripts name)))))))]
    {:status (if (seq failures) :failed :preflight-ok)
     :role role :agent agent :worktree (str worktree)
     :checked-at (str (java.time.Instant/now))
     :message (if (seq failures) (str/join "; " failures)
                  "Launcher environment checked; client tool-shell verification still required.")}))

(defn check! [root role worktree agent record?]
  (let [state (try (inspect root role worktree agent)
                   (catch Exception e {:status :failed :role role
                                       :checked-at (str (java.time.Instant/now))
                                       :message (ex-message e)}))]
    (when record? (write-state! root role state))
    state))

(defn issues [root]
  (let [dir (fs/path root ".swarmforge/startup")]
    (vec (when (fs/directory? dir)
           (for [file (fs/glob dir "*.edn")
                 :let [role (str/replace (fs/file-name file) #"\.edn$" "")
                       state (read-state root role)]
                 :when (= :failed (:status state))]
             {:role role :code "AGENT_STARTUP_FAILED" :message (:message state)})))))

(when (= (str *file*) (System/getProperty "babashka.file"))
  (let [[mode root role worktree agent] *command-line-args*]
    (when-not (and (#{"--check" "--record"} mode) root role worktree agent)
      (binding [*out* *err*]
        (println "Usage: bb swarm_startup.bb --check|--record <root> <role> <worktree> <agent>"))
      (System/exit 2))
    (let [state (check! root role worktree agent (= mode "--record"))]
      (println (if (= :failed (:status state)) "AGENT_STARTUP_FAILED:" "STARTUP_PREFLIGHT_OK:")
               role (:message state))
      (System/exit (if (= :failed (:status state)) 2 0)))))
