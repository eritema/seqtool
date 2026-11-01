#!/usr/bin/env bb
(ns container-git-trust
  (:require [babashka.fs :as fs]
            [babashka.process :as process]
            [clojure.string :as str]))

(defn runtime-context []
  (when-not (and (= "container" (System/getenv "SWARMFORGE_ISOLATION"))
                 (= "/home/node" (System/getenv "HOME"))
                 (= "/home/node/.gitconfig" (System/getenv "GIT_CONFIG_GLOBAL"))
                 (fs/regular-file? "/.dockerenv")
                 (fs/regular-file? "/etc/swarmforge-container"))
    (throw (ex-info "Git trust setup requires the dedicated container and its temporary Git config." {})))
  {:root (fs/path "/workspace") :config (fs/path "/home/node/.gitconfig")})

(defn git! [& args]
  (let [result (apply process/sh (concat [{:continue true} "/usr/bin/git"] args))]
    (when-not (zero? (:exit result))
      (throw (ex-info (str "Container Git setup failed: " (str/trim (:err result))) {:exit (:exit result)})))
    (:out result)))

(defn canonical [path]
  (if (fs/exists? path) (fs/real-path path) (fs/normalize path)))

(defn allowed-worktrees [root output]
  (let [root (canonical root)
        worktrees (fs/path root ".worktrees")]
    (vec (distinct
           (for [line (str/split-lines output)
                 :when (str/starts-with? line "worktree ")
                 :let [path (fs/path (subs line (count "worktree ")))
                       _ (when-not (fs/absolute? path)
                           (throw (ex-info "Git reported a relative worktree path; trust not granted." {})))
                       path (canonical path)]]
             (do
               (when-not (or (= root path) (.startsWith path worktrees))
                 (throw (ex-info "Git worktree is outside the mounted project's .worktrees directory; trust not granted." {})))
               path))))))

(defn prepare! []
  (let [{:keys [root config]} (runtime-context)
        root (canonical root)]
    (git! "config" "--file" (str config) "--add" "safe.directory" (str root))
    (when (fs/exists? (fs/path root ".git"))
      (let [paths (allowed-worktrees root (git! "-C" (str root) "worktree" "list" "--porcelain"))]
        (doseq [path paths :when (not= root path)]
          (git! "config" "--file" (str config) "--add" "safe.directory" (str path)))))
    (println "Container Git trust prepared for the mounted project and registered worktrees.")))

(when (= (str *file*) (System/getProperty "babashka.file"))
  (try (prepare!)
       (catch Exception e (binding [*out* *err*] (println (ex-message e))) (System/exit 1))))
