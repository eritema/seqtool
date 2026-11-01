#!/usr/bin/env bb
(ns agent-lifecycle
  (:require [babashka.fs :as fs]
            [clojure.edn :as edn]
            [clojure.string :as str])
  (:import [java.nio.channels FileChannel]
           [java.nio.file OpenOption StandardOpenOption]))

(def lock-options (into-array OpenOption [StandardOpenOption/CREATE StandardOpenOption/WRITE]))

(defn try-lock [channel]
  (try
    (.tryLock channel)
    (catch Exception e
      ;; Babashka exposes FileChannel but not this exception as an importable
      ;; class. Only an overlapping lock means contention; propagate IO errors.
      (if (= "java.nio.channels.OverlappingFileLockException" (.getName (class e)))
        nil
        (throw e)))))

(defn role-file [root role suffix]
  (when-not (re-matches #"[A-Za-z0-9_-]+" (or role ""))
    (throw (ex-info "Invalid lifecycle role" {})))
  (fs/path root ".swarmforge/agents" (str role suffix)))

(defn read-state [root role]
  (let [file (role-file root role ".edn")]
    (when (fs/regular-file? file)
      (try (let [state (edn/read-string (slurp (str file)))
                 exit-file (when (:generation state)
                             (fs/path (fs/parent file) "exits" (str role "-" (:generation state) ".edn")))]
             (if (and (not= :restart-failed (:status state)) exit-file (fs/regular-file? exit-file))
               (merge state (edn/read-string (slurp (str exit-file))) {:status :exited})
               state))
           (catch Exception _ {:status :restart-failed :message "Unreadable agent lifecycle state."})))))

(defn locked? [root role]
  (let [file (role-file root role ".lock")]
    (when (fs/exists? file)
      (with-open [channel (FileChannel/open file lock-options)]
        ;; Closing the channel releases its locks; FileLock.release is not
        ;; exposed by Babashka's interop.
        (if (try-lock channel) false true)))))

(defn with-role-lock! [root role f]
  (let [file (role-file root role ".lock")]
    (fs/create-dirs (fs/parent file))
    ;; OS locks release on process exit. Keep the file: deleting it would let
    ;; another process lock a different inode while this one is still locked.
    (with-open [channel (FileChannel/open file lock-options)]
      ;; As in the board/audit helpers, with-open releases the OS lock by
      ;; closing the channel, including when f throws. No FileLock interop.
      (if (try-lock channel)
        (f)
        (throw (ex-info "Role coordination is already in progress; retry after it finishes."
                        {:code "AGENT_COORDINATION_BUSY" :http-status 409}))))))

(defn record! [root role value]
  (let [file (role-file root role ".edn")
        history (fs/path (fs/parent file) "history")
        state (assoc value :version 1 :role role :at (str (java.time.Instant/now)))
        event (fs/path history (str role "-" (java.util.UUID/randomUUID) ".edn"))]
    (fs/create-dirs history)
    (let [tmp (fs/create-temp-file {:dir (fs/parent file) :prefix ".agent-"})]
      (spit (str tmp) (pr-str state))
      (spit (str event) (pr-str state))
      (fs/move tmp file {:replace-existing true}))
    state))

(defn record-exit! [root role generation code]
  ;; A late exit from an old process must not overwrite a replacement's state.
  ;; The generation-scoped marker also records exit if a reminder owns the lock.
  (when-not (re-matches #"[A-Za-z0-9_-]+" (or generation ""))
    (throw (ex-info "Invalid launch generation" {})))
  (let [file (fs/path (fs/parent (role-file root role ".edn")) "exits" (str role "-" generation ".edn"))]
    (fs/create-dirs (fs/parent file))
    (let [tmp (fs/create-temp-file {:dir (fs/parent file) :prefix ".exit-"})]
      (spit (str tmp) (pr-str {:generation generation :exit-code code :at (str (java.time.Instant/now))}))
      (fs/move tmp file {:replace-existing true})))
  (try
    (with-role-lock! root role
      (fn []
        (let [state (read-state root role)]
          (when (= generation (:generation state))
            (record! root role (assoc state :status :exited :exit-code code))))))
    (catch clojure.lang.ExceptionInfo e
      (when-not (= "AGENT_COORDINATION_BUSY" (:code (ex-data e))) (throw e)))))

(defn issues [root]
  (let [roles-file (fs/path root ".swarmforge/roles.tsv")]
    (vec (when (fs/regular-file? roles-file)
           (keep (fn [line]
                   (let [role (first (str/split line #"\t"))
                         state (when (seq role) (read-state root role))]
                     (when (#{:exited :restart-failed} (:status state))
                       {:role role :code (if (= :exited (:status state)) "AGENT_EXITED" "AGENT_RESTART_FAILED")
                        :message (or (:message state) (str "Agent exited with code " (:exit-code state) ". Other roles remain running."))
                        :next_action (str "Inspect the terminal, then run ./swarm --restart-role " role ".")})))
                 (str/split-lines (slurp (str roles-file))))))))

(when (= (str *file*) (System/getProperty "babashka.file"))
  (let [[mode root role generation code] *command-line-args*]
    (when-not (and (= mode "--exited") root role generation (re-matches #"[0-9]+" (or code "")))
      (throw (ex-info "Usage: agent_lifecycle.bb --exited <root> <role> <generation> <exit-code>" {})))
    (record-exit! root role generation (parse-long code))))
