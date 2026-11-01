#!/usr/bin/env bb
(ns swarm-container
  (:require [babashka.fs :as fs]
            [babashka.process :as process]
            [cheshire.core :as json]
            [clojure.string :as str]))

(def assets (str (fs/real-path (fs/path (fs/parent *file*) "container"))))
(load-file (str (fs/path assets "swarm_provider.bb")))
(load-file (str (fs/path assets "../runtime_snapshot.bb")))

(def trusted-root (fs/real-path (fs/path assets "../../..")))
(def codex-version "0.162.1")
(def bb-version "1.13.225")

(defn fail [message] (throw (ex-info message {})))
(defn within? [child parent] (.startsWith (fs/normalize child) (fs/normalize parent)))

(defn project-root! [path]
  (when-not (and path (fs/directory? path)) (fail "An existing, dedicated project directory is required."))
  (let [root (fs/real-path path)
        forbidden (map fs/real-path (filter fs/exists? ["/" "/tmp" (System/getProperty "user.home")]))]
    (when (or (some #{root} forbidden) (within? trusted-root root))
      (fail "Refusing to mount a broad host directory or the trusted SwarmForge source. Choose a separate project."))
    root))

(defn key-file! [root path]
  (when-not (and path (fs/regular-file? path) (fs/readable? path))
    (fail "A readable API key FILE outside the project is required. Never pass the key itself."))
  (let [key (fs/real-path path)
        project (fs/real-path root)]
    (when (within? key project) (fail "The API key file must be outside the mounted project."))
    ;; No slurp: the host launcher never reads or prints credential contents.
    key))

(defn sentinel-file! [root path]
  (when-not (and path (fs/absolute? (fs/path path)) (fs/regular-file? path))
    (fail "The harmless HOST sentinel must already exist at an absolute path."))
  (let [file (fs/real-path path)
        project (fs/real-path root)]
    (when (within? file project) (fail "The host sentinel must be outside the mounted project."))
    file))

(defn validate-project! [root]
  (when-not (and (fs/regular-file? (fs/path root "swarm"))
                (fs/regular-file? (fs/path root "swarmforge/swarmforge.conf")))
    (fail "Install a pack in the project before starting the container."))
  (when (fs/regular-file? (fs/path root ".git"))
    (fail "An external Git worktree cannot be mounted alone. Use a standalone repository."))
  (let [roles (fs/path root ".swarmforge/roles.tsv")]
    (when (fs/regular-file? roles)
      (doseq [line (remove str/blank? (str/split-lines (slurp (str roles))))
              :let [path (nth (str/split line #"\t" -1) 2 "")]]
        (when-not (within? path "/workspace")
          (fail "This project has host worktree paths. Use a fresh container project; no runtime state was deleted.")))))
  (swarm-provider/configured-roles
    (slurp (str (fs/path root "swarmforge/swarmforge.conf"))))
  nil)

(defn provider-requirements [root]
  (swarm-provider/requirements
    (swarm-provider/configured-roles
      (slurp (str (fs/path root "swarmforge/swarmforge.conf"))))))

(defn local-options! [root requirements]
  (if (:lmstudio? requirements)
    (let [port (or (System/getenv "SWARMFORGE_LMSTUDIO_PORT") "1234")
          max-output (or (System/getenv "SWARMFORGE_LMSTUDIO_MAX_OUTPUT_TOKENS") "16384")]
      (when-not (and (re-matches #"[0-9]+" port) (<= 1024 (parse-long port) 65535))
        (fail "SWARMFORGE_LMSTUDIO_PORT must be between 1024 and 65535."))
      (when-not (and (re-matches #"[0-9]{1,10}" max-output)
                     (pos? (parse-long max-output)))
        (fail "SWARMFORGE_LMSTUDIO_MAX_OUTPUT_TOKENS must be a positive integer of at most 10 digits."))
      (assoc requirements :lmstudio-port port
             :lmstudio-max-output-tokens max-output
             :lmstudio-token (key-file! root (System/getenv "SWARMFORGE_LMSTUDIO_TOKEN_FILE"))))
    requirements))

(defn identity-for [root]
  (let [digest (.digest (java.security.MessageDigest/getInstance "SHA-256") (.getBytes (str root) "UTF-8"))]
    (str "swarmforge-" (subs (apply str (map #(format "%02x" (bit-and 255 %)) digest)) 0 16))))

(defn host-id [option]
  (let [result (process/sh {:continue true} "id" option)
        value (str/trim (:out result))]
    (when-not (and (zero? (:exit result)) (re-matches #"[1-9][0-9]*" value))
      (fail "Container preparation requires a non-root host user with numeric UID/GID."))
    value))

(defn port! [value]
  (when-not (and (re-matches #"[0-9]+" value) (<= 1024 (parse-long value) 65535))
    (fail "SWARMFORGE_DASHBOARD_PORT must be between 1024 and 65535."))
  value)

(defn images [uid gid]
  {:worker (str "swarmforge-worker:codex-" codex-version "-bb-" bb-version "-u" uid "-g" gid)
   :proxy "swarmforge-proxy:bookworm"})

(def hardening {:read_only true :cap_drop ["ALL"] :security_opt ["no-new-privileges:true"]
                :pids_limit 512 :restart "no"})

(defn build-spec [uid gid]
  (let [images (images uid gid)]
    {:services
     {:worker {:image (:worker images)
               :build {:context assets :dockerfile "worker.Dockerfile"
                       :args {:SWARM_UID uid :SWARM_GID gid :CODEX_VERSION codex-version :BABASHKA_VERSION bb-version}}}
      :proxy {:image (:proxy images) :build {:context assets :dockerfile "proxy.Dockerfile"}}}}))

(defn runtime-spec
  ([root key uid gid port] (runtime-spec root key uid gid port {:openai? true}))
  ([root key uid gid port options]
  (let [image (images uid gid) name (identity-for root)]
    (cond->
      {:services
       {:worker (merge hardening
                       {:image (:worker image) :container_name (str name "-worker")
                        :user (str uid ":" gid) :mem_limit "4g"
                        :volumes [{:type "bind" :source (str root) :target "/workspace"}]
                        :tmpfs ["/tmp:rw,nosuid,nodev,size=512m,mode=1777"
                                (str "/home/node:rw,nosuid,nodev,size=512m,mode=0700,uid=" uid ",gid=" gid)]
                        :dns ["127.0.0.1"]
                        :networks ["isolated"] :depends_on ["proxy"]})
        :proxy (merge hardening
                      {:image (:proxy image) :container_name (str name "-proxy") :mem_limit "256m"
                       :tmpfs ["/tmp:rw,nosuid,nodev,size=64m,mode=1777"]
                       :networks ["isolated" "egress"]})
        :dashboard (merge hardening
                          {:image (:proxy image) :container_name (str name "-dashboard")
                           :mem_limit "64m" :pids_limit 128
                           :entrypoint ["/usr/bin/socat"]
                           :command ["TCP-LISTEN:8080,bind=0.0.0.0,reuseaddr,fork" "TCP:worker:8080"]
                           :ports [(str "127.0.0.1:" port ":8080")]
                           :depends_on ["worker"]
                           :networks ["isolated" "egress"]})}
       :networks {:isolated {:internal true} :egress {}}}
      key (assoc :secrets {:openai_api_key {:file (str key)}})
      key (assoc-in [:services :worker :secrets] ["openai_api_key"])
      (not (:openai? options)) (update :services dissoc :proxy)
      (:lmstudio? options)
      (assoc-in [:services :lmstudio]
                (merge hardening
                       {:image (:worker image) :container_name (str name "-lmstudio")
                        :user (str uid ":" gid) :mem_limit "256m"
                        :entrypoint ["/usr/local/bin/node" "/opt/swarmforge/lmstudio_gateway.cjs"]
                        :environment {:SWARMFORGE_LMSTUDIO_PORT (:lmstudio-port options)
                                      :SWARMFORGE_LMSTUDIO_MAX_OUTPUT_TOKENS (or (:lmstudio-max-output-tokens options) "16384")}
                        :secrets ["lmstudio_token"] :networks ["isolated" "egress"]
                        :tmpfs ["/tmp:rw,nosuid,nodev,size=32m,mode=1777"]}))
      (:lmstudio? options)
      (assoc-in [:secrets :lmstudio_token] {:file (str (:lmstudio-token options))})
      (:lmstudio? options)
      (assoc-in [:services :worker :environment]
                {:SWARMFORGE_OPENAI_ENABLED (if (:openai? options) "1" "0")
                 :SWARMFORGE_LMSTUDIO_ENABLED "1"
                 :SWARMFORGE_LMSTUDIO_PORT (:lmstudio-port options)
                 :NO_PROXY "127.0.0.1,localhost,lmstudio"
                 :no_proxy "127.0.0.1,localhost,lmstudio"})
      true (assoc-in [:services :worker :depends_on]
                     (vec (concat (when (:openai? options) ["proxy"])
                                  (when (:lmstudio? options) ["lmstudio"]))))
      (:runtime-snapshot options)
      (assoc-in [:services :worker :entrypoint]
                ["/usr/bin/tini" "--"
                 "/opt/swarmforge/runtime/swarmforge/scripts/container/entrypoint.sh"])
      (:runtime-snapshot options)
      (update-in [:services :worker :volumes] into
                 [{:type "bind" :source (:runtime-snapshot options)
                   :target runtime-snapshot/container-root :read_only true
                   :bind {:create_host_path false}}
                  {:type "bind" :source (str (fs/path (:runtime-snapshot options) "project/swarmforge"))
                   :target "/workspace/swarmforge" :read_only true
                   :bind {:create_host_path false}}
                  {:type "bind" :source (str (fs/path (:runtime-snapshot options) "project/swarm"))
                   :target "/workspace/swarm" :read_only true
                   :bind {:create_host_path false}}])))))

(defn inspection-spec [root uid gid port]
  ;; Metadata commands must see the optional gateway even in a new host shell
  ;; with no credential variables. This descriptor is never used by up/run.
  (assoc-in (runtime-spec root nil uid gid port) [:services :lmstudio]
            {:image (:worker (images uid gid))
             :container_name (str (identity-for root) "-lmstudio")}))

(defn compose! [name spec & args]
  (let [result (apply process/sh (concat [{:in (json/generate-string spec) :out :inherit :err :inherit :continue true}
                                        "docker" "compose" "--project-name" name "-f" "-"] args))]
    (when-not (zero? (:exit result))
      (throw (ex-info "Docker Compose failed. Inspect the output above; no native fallback was attempted."
                      {:exit (:exit result)})))))

(defn exec! [name & command]
  (let [result (apply process/sh (concat [{:out :inherit :err :inherit :continue true}
                                        "docker" "exec" (str name "-worker")] command))]
    (when-not (zero? (:exit result))
      (throw (ex-info "Container command failed; no host execution was attempted." {:exit (:exit result)})))))

(def recovery-input-program
  "(load-string (slurp *in*)) (apply recovery-control/-main *command-line-args*)")

(defn recovery! [name role action]
  (let [source (slurp (str (fs/path trusted-root "swarmforge/scripts/recovery_control.bb")))
        result (process/sh {:in source :out :inherit :err :inherit :continue true}
                 "docker" "exec" "-i" "-w" "/workspace" (str name "-worker")
                 "bb" "-e" recovery-input-program "--" "/workspace" role action)]
    (when-not (zero? (:exit result))
      (throw (ex-info "Container recovery command failed; no host execution was attempted."
                      {:exit (:exit result)})))))

(defn -main [& args]
  (let [[action path credential extra] args
        uid (host-id "-u") gid (host-id "-g")
        port (port! (or (System/getenv "SWARMFORGE_DASHBOARD_PORT") "8080"))]
    (if (= "build" action)
      (do (when-not (= 1 (count args)) (fail "Usage: swarm-container build"))
          (compose! "swarmforge-build" (build-spec uid gid) "build"))
      (let [root (project-root! path)
            name (identity-for root)]
        (case action
          ("plan" "start")
          (do
            (when-not (<= 2 (count args) 3) (fail "Usage: swarm-container plan|start PROJECT [OPENAI_KEY_FILE]"))
            (validate-project! root)
            (let [options (local-options! root (provider-requirements root))
                  _ (when (and credential (not (:openai? options)))
                      (fail "Local-only projects do not accept an OpenAI key file."))
                  key (when (:openai? options) (key-file! root credential))
                  snapshot (runtime-snapshot/prepare! root trusted-root)
                  _ (runtime-snapshot/verify! snapshot)
                  spec (runtime-spec root key uid gid port (assoc options :runtime-snapshot snapshot))]
              (if (= action "plan")
                (println (json/generate-string spec {:pretty true}))
                (do (compose! name spec "up" "-d" "--no-build" "--pull" "never")
                    (println (str "Read-only runtime snapshot: " snapshot))
                    (println (str "Container start requested. Dashboard: http://127.0.0.1:" port))
                    (println "Use status/logs to verify readiness. No native agents were started.")))))
          "stop" (do (when-not (= 2 (count args)) (fail "Usage: swarm-container stop PROJECT"))
                     (compose! name (runtime-spec root nil uid gid port) "down" "--remove-orphans" "--timeout" "15"))
          "status" (compose! name (inspection-spec root uid gid port) "ps" "-a")
          "logs" (compose! name (inspection-spec root uid gid port) "logs" "--tail" "80")
          "restart-role" (do
                           (when-not (and (= 3 (count args)) (re-matches #"[A-Za-z0-9-]+" credential))
                             (fail "Usage: swarm-container restart-role PROJECT ROLE"))
                           (exec! name "/bin/sh" "-c"
                                  "if [ -f /opt/swarmforge/runtime/runtime.edn ]; then exec /opt/swarmforge/runtime/swarm \"$@\"; else exec ./swarm \"$@\"; fi"
                                  "restart-role" "--restart-role" credential))
          ("pause-recovery" "resume-recovery")
          (do
            (when-not (and (= 3 (count args)) (re-matches #"[A-Za-z0-9-]+" (or credential "")))
              (fail "Usage: swarm-container pause-recovery|resume-recovery PROJECT ROLE"))
            (recovery! name credential action))
          "check" (do
                    (when-not (and (= 3 (count args)) (str/starts-with? credential "/"))
                      (fail "Usage: swarm-container check PROJECT HOST_SENTINEL_ABSOLUTE_PATH"))
                    (exec! name "bb" "/opt/swarmforge/isolation_probe.bb" (str (sentinel-file! root credential))))
          "probe" (do
                    ;; The operator runs this explicitly; no live model calls.
                    (when-not (and (= 3 (count args)) (str/starts-with? credential "/"))
                      (fail "Usage: swarm-container probe PROJECT HOST_SENTINEL_ABSOLUTE_PATH"))
                    (validate-project! root)
                    (let [snapshot (runtime-snapshot/prepare! root trusted-root)]
                      (runtime-snapshot/verify! snapshot)
                      (compose! name (runtime-spec root nil uid gid port
                                                  (assoc (local-options! root (provider-requirements root))
                                                         :runtime-snapshot snapshot))
                              "run" "--rm" "--no-TTY" "--pull" "never" "worker" "--probe"
                              (str (sentinel-file! root credential)))))
          "check-runtime" (do
                            (when-not (= 2 (count args)) (fail "Usage: swarm-container check-runtime PROJECT"))
                            (exec! name "bb" "/opt/swarmforge/runtime/swarmforge/scripts/runtime_snapshot.bb"
                                   "--check-container"))
          (fail "Usage: swarm-container build | plan/start PROJECT [OPENAI_KEY_FILE] | status/logs/stop/check-runtime PROJECT | restart-role/pause-recovery/resume-recovery PROJECT ROLE | probe/check PROJECT HOST_SENTINEL"))))))

(when (= (str *file*) (System/getProperty "babashka.file"))
  (try (apply -main *command-line-args*)
       (catch Exception e
         (binding [*out* *err*] (println (ex-message e)))
         (System/exit (or (:exit (ex-data e)) 1)))))
