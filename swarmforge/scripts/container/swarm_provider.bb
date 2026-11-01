#!/usr/bin/env bb
(ns swarm-provider
  (:require [clojure.string :as str]))

(defn fail [message] (throw (ex-info message {})))

(defn container-options [extra]
  (when (re-find #"[\n\r;`$|&<>]" (or extra ""))
    (fail "Container role arguments cannot contain shell operators or substitutions."))
  (let [tokens (remove str/blank? (str/split (or extra "") #"\s+"))]
    (loop [remaining tokens oss? false local nil kept []]
      (if-let [token (first remaining)]
        (cond
          (= token "--oss")
          (do (when oss? (fail "Duplicate --oss in container role."))
              (recur (next remaining) true local kept))
          (= token "--local-provider")
          (do (when (or local (not= "lmstudio" (second remaining)))
                (fail "Container local roles require exactly --oss --local-provider lmstudio."))
              (recur (nnext remaining) oss? "lmstudio" kept))
          (str/starts-with? token "--local-provider=")
          (do (when (or local (not= token "--local-provider=lmstudio"))
                (fail "Container local roles support LM Studio only."))
              (recur (next remaining) oss? "lmstudio" kept))
          :else (recur (next remaining) oss? local (conj kept token)))
        (let [cleaned (str/join " " kept)]
          (when (not= oss? (boolean local))
            (fail "Container local roles require both --oss and --local-provider lmstudio."))
          (when (and local (re-find #"(?:^|\s)--search(?:=|\s|$)" cleaned))
            (fail "LM Studio container roles do not support hosted web search; remove --search."))
          (when (re-find #"(?:--oss|--local-provider|--remote|--profile)(?:=|\s|$)|model_provider|base_url|--yolo|bypass|--sandbox|--ask-for-approval|(?:^|\s)-[psa]" cleaned)
            (fail "Container roles cannot override provider endpoints, profiles or permission policy."))
          {:provider (if local :lmstudio :openai)
           :extra-args (not-empty cleaned)})))))

(defn configured-roles [text]
  (let [rows (for [raw (str/split-lines text)
                   :let [line (str/trim raw)]
                   :when (and (seq line) (not (str/starts-with? line "#")))]
               (let [[directive role agent worktree & extra] (str/split line #"\s+")
                     extra (if (#{"task" "batch"} (first extra)) (rest extra) extra)
                     extra (if (#{"forward-only" "back-one" "back-all"} (first extra)) (rest extra) extra)]
                 (when-not (and (#{"window" "window-invisible"} directive)
                                (re-matches #"[A-Za-z0-9-]+" (or role ""))
                                (= "codex" agent) (not (str/blank? worktree)))
                   (fail "Container mode supports Codex pack roles only, not forge hosts or other clients."))
                 (assoc (container-options (str/join " " extra)) :role role)))]
    (when (empty? rows) (fail "No Codex pack roles configured."))
    (when-not (= (count rows) (count (distinct (map :role rows))))
      (fail "Duplicate role in container configuration."))
    (vec rows)))

(defn requirements [roles]
  {:openai? (boolean (some #(= :openai (:provider %)) roles))
   :lmstudio? (boolean (some #(= :lmstudio (:provider %)) roles))})
