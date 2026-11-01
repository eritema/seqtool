#!/usr/bin/env bb
(ns runtime-snapshot
  (:require [babashka.fs :as fs]
            [clojure.edn :as edn]
            [clojure.java.io :as io]))

;; Host-side only. The store must never be underneath the writable project.
;; The container sees only an immutable snapshot, not the store or source repo.
(def container-root "/opt/swarmforge/runtime")

(defn fail! [message] (throw (ex-info (str "RUNTIME_PROTECTION: " message) {})))

(defn sha256 [file]
  (let [digest (java.security.MessageDigest/getInstance "SHA-256")
        buffer (byte-array 65536)]
    (with-open [input (io/input-stream (str file))]
      (loop []
        (let [n (.read input buffer)]
          (when (pos? n) (.update digest buffer 0 n) (recur)))))
    (apply str (map #(format "%02x" (bit-and 255 %)) (.digest digest)))))

(defn files [directory]
  (when (fs/sym-link? directory) (fail! (str "Symlink refused: " directory)))
  (when-not (fs/directory? directory) (fail! (str "Directory missing: " directory)))
  (mapcat (fn [entry]
            (cond
              (fs/sym-link? entry) (fail! (str "Symlink refused: " entry))
              (fs/directory? entry) (files entry)
              (fs/regular-file? entry) [entry]
              :else (fail! (str "Special file refused: " entry))))
          (sort (fs/list-dir directory))))

(defn executable-bits? [file]
  ;; Files.isExecutable/access(X_OK) can report true through Docker Desktop
  ;; even for 0644 files. Compare stored POSIX mode bits, not effective access
  ;; for the current OS/user. Unsupported permission views must fail closed.
  (boolean (some #{"OWNER_EXECUTE" "GROUP_EXECUTE" "OTHERS_EXECUTE"}
                 (map str (fs/posix-file-permissions file)))))

(defn entry [file]
  {:sha256 (sha256 file) :executable (executable-bits? file)})

(defn sources [project trusted]
  (let [installed (fs/path project "swarmforge")
        scripts (fs/path trusted "swarmforge/scripts")
        ;; Preserve operator configuration/policies, but always deploy helpers
        ;; from the selected trusted checkout, not from an agent's worktree.
        installed-files (vec (files installed))
        policies (for [file installed-files
                       :let [relative (str (fs/relativize installed file))]
                       :when (not (or (= relative "scripts")
                                      (.startsWith relative "scripts/")))]
                   [(str "swarmforge/" relative) file])
        helpers (for [file (files scripts)]
                  [(str "swarmforge/scripts/" (fs/relativize scripts file)) file])
        ;; Freeze the existing Git-path view byte-for-byte, separately from
        ;; the active runtime. An upgrade must not introduce dirty Git files
        ;; or silently rewrite the project's tracked runtime baseline.
        aliases (for [file installed-files]
                  [(str "project/swarmforge/" (fs/relativize installed file)) file])
        launcher (fs/path trusted "swarm")
        installed-launcher (fs/path project "swarm")]
    (doseq [file [launcher installed-launcher]]
      (when (or (fs/sym-link? file) (not (fs/regular-file? file)))
        (fail! (str "Launcher is missing or a symlink: " file))))
    (into (sorted-map) (concat policies helpers aliases
                               [["swarm" launcher] ["project/swarm" installed-launcher]]))))

(defn store-directory []
  (fs/path (System/getProperty "user.home") ".local/share/swarmforge/runtimes"))

(defn outside-project! [project store]
  ;; Resolve existing parents as well: a symlink in a not-yet-created store
  ;; must not hide a writable-project alias.
  (fs/create-dirs store)
  (let [project (fs/real-path project) store (fs/real-path store)]
    (when (.startsWith store project)
      (fail! "Snapshot store must be outside the writable project."))
    store))

(declare verify!)

(defn prepare! [project trusted]
  (let [store (outside-project! project (store-directory))
        source-map (sources project trusted)
        manifest (into (sorted-map) (map (fn [[path file]] [path (entry file)]) source-map))
        staging (fs/create-temp-dir {:dir store :prefix ".prepare-"})]
    (try
      (doseq [[path source] source-map]
        (let [destination (fs/path staging path)]
          (fs/create-dirs (fs/parent destination))
          (fs/copy source destination {:copy-attributes true})))
      ;; Reject concurrent edits of either source tree during the copy.
      (when-not (= manifest (into (sorted-map)
                                 (map (fn [[path _]] [path (entry (fs/path staging path))]) source-map)))
        (fail! "Source changed while preparing the snapshot. Retry from a stable checkout."))
      (spit (str (fs/path staging "runtime.edn")) (pr-str {:version 1 :files manifest}))
      (let [id (sha256 (fs/path staging "runtime.edn"))
            destination (fs/path store (str "snapshot-" id))]
        ;; Content-addressed snapshots: an unchanged start does not recreate
        ;; the worker. Never overwrite an existing or running mount.
        (if (fs/exists? destination)
          (do
            (when (fs/sym-link? destination) (fail! "Snapshot destination is a symlink."))
            (when-not (= (entry (fs/path staging "runtime.edn"))
                         (entry (fs/path destination "runtime.edn")))
              (fail! "Existing snapshot manifest differs."))
            (doseq [[path expected] manifest]
              (let [file (fs/path destination path)]
                (when (or (fs/sym-link? file) (not (fs/regular-file? file))
                          (not= expected (entry file)))
                  (fail! (str "Existing snapshot was modified: " path))))))
          (fs/move staging destination))
        (verify! destination)
        (str destination))
      (finally (when (fs/exists? staging) (fs/delete-tree staging))))))

(defn verify! [snapshot]
  (let [manifest-file (fs/path snapshot "runtime.edn")
        value (edn/read-string (slurp (str manifest-file)))
        expected (:files value)
        actual (into (sorted-map)
                     (for [file (files snapshot)
                           :let [relative (str (fs/relativize snapshot file))]
                           :when (not= relative "runtime.edn")]
                       [relative (entry file)]))]
    (when-not (and (= 1 (:version value)) (map? expected) (seq expected))
      (fail! "Invalid snapshot manifest."))
    (when-not (= expected actual)
      (let [different (vec (for [path (sort (distinct (concat (keys expected) (keys actual))))
                                :when (not= (get expected path) (get actual path))]
                            {:path path :expected (get expected path) :actual (get actual path)}))]
        (fail! (str "Snapshot manifest does not match its files (" (count different)
                    " differences). First entries: " (pr-str (take 5 different))))))
    true))

(defn append-open-denied? [file]
  ;; No truncation or writes, even if the protection check fails.
  (try
    (with-open [_ (io/output-stream (str file) :append true)] false)
    (catch java.io.IOException _ true)))

(defn check-container! []
  (verify! container-root)
  (let [manifest (:files (edn/read-string (slurp (str (fs/path container-root "runtime.edn")))))]
    (doseq [[path expected] manifest
            :when (.startsWith path "project/")]
      (let [alias (fs/path "/workspace" (subs path (count "project/")))]
        (when (or (fs/sym-link? alias) (not (fs/regular-file? alias))
                  (not= expected (entry alias)))
          (fail! (str "Project runtime alias does not match snapshot: " path))))))
  (doseq [file [(fs/path container-root "swarm")
                (fs/path container-root "swarmforge/scripts/swarmforge.bb")
                (fs/path container-root "swarmforge/scripts/pack/dashboard.html")
                (fs/path container-root "swarmforge/swarmforge.conf")
                (fs/path "/workspace/swarm")
                (fs/path "/workspace/swarmforge/scripts/pack/dashboard.html")
                (fs/path "/workspace/swarmforge/swarmforge.conf")]]
    (when-not (and (fs/regular-file? file) (append-open-denied? file))
      (fail! (str "Runtime is missing or writable: " file))))
  (println "PASS: runtime manifest matches and active runtime/project aliases deny writes."))

(when (= (str *file*) (System/getProperty "babashka.file"))
  (try
    (if (= ["--check-container"] (vec *command-line-args*))
      (check-container!)
      (fail! "Usage: bb runtime_snapshot.bb --check-container"))
    (catch Exception e
      (binding [*out* *err*] (println (ex-message e)))
      (System/exit 1))))
