#!/usr/bin/env bb
(ns container-codex-auth
  (:require [babashka.fs :as fs]
            [clojure.string :as str]))

(defn runtime-context []
  (when-not (and (= "container" (System/getenv "SWARMFORGE_ISOLATION"))
                 (= "/home/node" (System/getenv "HOME"))
                 (= "/home/node/.codex" (System/getenv "CODEX_HOME"))
                 (fs/regular-file? "/.dockerenv")
                 (fs/regular-file? "/etc/swarmforge-container"))
    (throw (ex-info "Codex provider setup requires the dedicated container home." {})))
  {:home (fs/path "/home/node/.codex")
   :key (System/getenv "SWARMFORGE_OPENAI_API_KEY")
   :openai? (not= "0" (System/getenv "SWARMFORGE_OPENAI_ENABLED"))
   :lmstudio? (= "1" (System/getenv "SWARMFORGE_LMSTUDIO_ENABLED"))})

(defn prepare! []
  (let [{:keys [home key lmstudio?] :as ctx} (runtime-context)]
    ;; Never put the credential in config, command arguments, errors or logs.
    (when (and (get ctx :openai? true) (or (str/blank? key) (re-find #"\s" key)))
      (throw (ex-info "The dedicated API key is empty or contains whitespace; no agents started." {})))
    (fs/create-dirs home)
    (spit (str (fs/path home "config.toml"))
          (str "model_provider = \"swarmforge_" (if (get ctx :openai? true) "openai" "lmstudio") "\"\n\n"
               (when (get ctx :openai? true)
                 (str "[model_providers.swarmforge_openai]\n"
                      "name = \"SwarmForge OpenAI\"\n"
                      "base_url = \"https://api.openai.com/v1\"\n"
                      "wire_api = \"responses\"\n"
                      "env_key = \"SWARMFORGE_OPENAI_API_KEY\"\n"
                      "requires_openai_auth = false\n"))
               (when lmstudio?
                 (str "\n[model_providers.swarmforge_lmstudio]\n"
                      "name = \"SwarmForge LM Studio gateway\"\n"
                      "base_url = \"http://lmstudio:1234/v1\"\n"
                      "wire_api = \"responses\"\n"
                      "stream_idle_timeout_ms = 600000\n"
                      "requires_openai_auth = false\n"))))
    (println "Container Codex provider prepared with secret-backed environment authentication.")))

(when (= (str *file*) (System/getProperty "babashka.file"))
  (try (prepare!)
       (catch Exception e
         (binding [*out* *err*] (println (ex-message e)))
         (System/exit 1))))
