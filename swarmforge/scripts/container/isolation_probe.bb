#!/usr/bin/env bb
(ns isolation-probe
  (:require [babashka.fs :as fs]
            [babashka.process :as process]
            [clojure.java.io :as io]
            [clojure.string :as str]))

;; Explicit operator-run checks. No models, secrets, installs or host exports.
(defn check! [label result]
  (when-not result (throw (ex-info (str "FAIL: " label) {})))
  (println "PASS:" label))

(defn denied-write? [path]
  (try (spit path "swarmforge-isolation-probe\n") false
       (catch Exception _ true)))

(defn read-process-status [source]
  ;; Parse a sequential stream; production supplies captured cat output so
  ;; the native-image JVM does not open procfs itself.
  (try
    (with-open [reader (io/reader source)]
      (into {} (keep (fn [line]
                       (when-let [[_ key value] (re-matches #"([^:]+):\s*(.*)" line)]
                         [key (str/trim value)])))
            (line-seq reader)))
    (catch Exception e
      (throw (ex-info (str "Cannot read process privilege status: " (ex-message e)) {} e)))))

(defn current-process-id [] (.pid (java.lang.ProcessHandle/current)))

(defn current-process-status []
  ;; /proc/self/status in the child would describe cat, not this probe.
  ;; Pass the parent's explicit PID, with no shell or privilege escalation.
  (let [path (str "/proc/" (current-process-id) "/status")
        result (process/sh {:continue true} "/bin/cat" path)]
    (when-not (zero? (:exit result))
      (throw (ex-info (str "Cannot read probe privileges via cat: " (str/trim (:err result)))
                      {:exit (:exit result)})))
    (read-process-status (java.io.StringReader. (:out result)))))

(defn check-privileges! [status]
  (check! "privilege escalation disabled" (= "1" (get status "NoNewPrivs")))
  (check! "no effective Linux capabilities" (boolean (re-matches #"0+" (get status "CapEff" "")))))

(defn curl [& args]
  (apply process/sh (concat [{:continue true} "curl" "--silent" "--show-error"
                            "--noproxy" ""
                            "--connect-timeout" "5" "--max-time" "15"
                            "--output" "/dev/null"] args)))

(defn -main [sentinel]
  (when-not (and sentinel (.isAbsolute (fs/path sentinel))
                 (not (.startsWith (fs/normalize sentinel) (fs/path "/workspace"))))
    (throw (ex-info "Supply the absolute path of a harmless HOST sentinel outside the mounted project." {})))
  (check! "container marker" (fs/regular-file? "/etc/swarmforge-container"))
  (check! "host sentinel invisible" (not (fs/exists? sentinel)))
  (check! "host sentinel write denied" (denied-write? (str sentinel ".container-write-probe")))
  (check! "host Docker socket absent" (not (fs/exists? "/var/run/docker.sock")))
  (check! "container root filesystem read-only" (denied-write? "/etc/swarmforge-write-probe"))
  (check-privileges! (current-process-status))
  (let [file "/workspace/.swarmforge/isolation-probe.txt"]
    (fs/create-dirs (fs/parent file))
    (spit file "authorized project write\n")
    (check! "project remains writable" (= "authorized project write\n" (slurp file))))
  (let [allowed (curl "--write-out" "%{http_connect} %{http_code}" "https://api.openai.com/v1/models")
        rejected (curl "--write-out" "%{http_connect}" "https://example.com")
        private (curl "--write-out" "%{http_connect}" "https://127.0.0.1")
        direct (curl "--noproxy" "*" "http://1.1.1.1")
        dns (process/sh {:continue true} "getent" "hosts" "example.com")]
    (if (not= "0" (System/getenv "SWARMFORGE_OPENAI_ENABLED"))
      (do (check! "OpenAI reachable through proxy without credentials"
                  (and (zero? (:exit allowed)) (= "200 401" (str/trim (:out allowed)))))
          (check! "other HTTPS destinations denied by proxy" (= "403" (str/trim (:out rejected))))
          (check! "private destinations denied by proxy" (= "403" (str/trim (:out private)))))
      (check! "OpenAI and other proxy destinations unavailable in local-only mode"
              (every? #(not (zero? (:exit %))) [allowed rejected private])))
    (check! "direct public network request denied" (not (zero? (:exit direct))))
    (check! "external DNS resolution denied in worker" (and (not (zero? (:exit dns))) (str/blank? (:out dns)))))
  (when (= "1" (System/getenv "SWARMFORGE_LMSTUDIO_ENABLED"))
    (let [models (curl "--noproxy" "*" "--write-out" "%{http_code}" "http://lmstudio:1234/v1/models")
          admin (curl "--noproxy" "*" "--write-out" "%{http_code}" "http://lmstudio:1234/api/v1/models")
          direct (curl "--noproxy" "*"
                       (str "http://host.docker.internal:"
                            (or (System/getenv "SWARMFORGE_LMSTUDIO_PORT") "1234") "/v1/models"))]
      (check! "LM Studio models reachable through authenticated gateway"
              (and (zero? (:exit models)) (= "200" (str/trim (:out models)))))
      (check! "LM Studio administrative routes denied" (= "403" (str/trim (:out admin))))
      (check! "direct host LM Studio access denied" (not (zero? (:exit direct))))))
  (println "Probe complete. Host sentinel contents must also be checked on the host."))

(when (= (str *file*) (System/getProperty "babashka.file"))
  (try (apply -main *command-line-args*)
       (catch Exception e (binding [*out* *err*] (println (ex-message e))) (System/exit 1))))
