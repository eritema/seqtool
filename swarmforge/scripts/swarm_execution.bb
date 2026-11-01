#!/usr/bin/env bb
(ns swarm-execution
  (:require [babashka.fs :as fs]))

(load-file (str (fs/path (fs/parent *file*) "container/swarm_provider.bb")))

(defn container? []
  ;; A user environment variable alone must not select unrestricted execution.
  (and (= "container" (System/getenv "SWARMFORGE_ISOLATION"))
       (fs/regular-file? "/.dockerenv")
       (fs/regular-file? "/etc/swarmforge-container")))

(defn codex-provider-options [extra]
  (if (container?)
    (swarm-provider/container-options extra)
    {:provider :native :extra-args extra}))

(defn provider-enabled? [provider]
  (case provider
    :lmstudio (= "1" (System/getenv "SWARMFORGE_LMSTUDIO_ENABLED"))
    :openai (not= "0" (System/getenv "SWARMFORGE_OPENAI_ENABLED"))))

(defn codex-provider-args [extra quote-arg]
  (let [{:keys [provider extra-args]} (codex-provider-options extra)]
    (str (when extra-args (str extra-args " "))
         (when (not= :native provider)
           (when (and (= :lmstudio provider) (not (provider-enabled? provider)))
             (throw (ex-info "LM Studio gateway is not enabled; recreate the container with its token file." {})))
           (when (and (= :openai provider) (not (provider-enabled? provider)))
             (throw (ex-info "OpenAI is not enabled in this local-only container." {})))
           (str "-c " (quote-arg (str "model_provider=\"swarmforge_" (name provider) "\"")) " "
                ;; OpenAI's hosted web tool is unavailable through this gateway.
                ;; Follow user extras so inherited cached/live defaults cannot
                ;; add an unsupported tool to local Responses requests.
                (when (= :lmstudio provider)
                  (str "-c " (quote-arg "web_search=\"disabled\"") " ")))))))

(defn codex-args [root quote-arg]
  (if (container?)
    ;; Opt-in outer Docker isolation. Do not request privileged nested bwrap.
    "--sandbox danger-full-access --ask-for-approval never "
    (str "--sandbox workspace-write --ask-for-approval on-request --add-dir "
         (quote-arg (str root)) " ")))

(defn dashboard-bind []
  (if (container?) "0.0.0.0" "127.0.0.1"))

(defn dashboard-port [] (when (container?) "8080"))

(defn validate-container-home! [home dir]
  (when-not (and (container?)
                 (= "/home/node/.codex" (str (fs/normalize home)))
                 (.startsWith (fs/normalize dir) (fs/path "/workspace")))
    (throw (ex-info "Automatic Codex trust is restricted to the dedicated container home and workspace." {}))))
